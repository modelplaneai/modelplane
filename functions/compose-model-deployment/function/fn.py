# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Fan out a ModelDeployment to ModelReplicas and ModelEndpoints.

This function discovers InferenceClusters, matches the deployment's
engines against available capacity, creates a ModelReplica per
selected cluster, and creates one ModelEndpoint per replica for
ModelService to route to.
"""

import enum

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function import cel, name, scheduling

# Condition types and reasons for the ModelDeployment XR.
CONDITION_TYPE_REPLICAS_SCHEDULED = "ReplicasScheduled"
CONDITION_TYPE_REPLICAS_READY = "ReplicasReady"
# ModelCacheResolved reports whether the referenced ModelCache could be read.
# It's independent of ReplicasScheduled: a deployment can hold retained replicas
# (scheduled) while its cache is momentarily unresolved (not resolved).
CONDITION_TYPE_MODEL_CACHE_RESOLVED = "ModelCacheResolved"

CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_CACHE_RESOLVED = "ModelCacheResolved"
CONDITION_REASON_CACHE_UNRESOLVED = "ModelCacheUnresolved"
CONDITION_REASON_CACHE_NOT_FOUND = "ModelCacheNotFound"
CONDITION_REASON_INSUFFICIENT_CAPACITY = "InsufficientCapacity"
CONDITION_REASON_INVALID_NODE_SELECTOR = "InvalidNodeSelector"
CONDITION_REASON_REPLICAS_CREATED = "ReplicasCreated"
CONDITION_REASON_SCHEDULING = "Scheduling"
CONDITION_REASON_NO_REPLICAS_SCHEDULED = "NoReplicasScheduled"
CONDITION_REASON_ALL_REPLICAS_READY = "AllReplicasReady"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Label keys stamped on the ModelReplicas and ModelEndpoints this function
# composes, identifying the deployment and cluster they belong to.
_LABEL_CLUSTER = "modelplane.ai/cluster"
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"
# Per-cluster-local index distinguishing co-located replicas of one deployment.
# Read back by the scheduler to reconstruct a replica's (cluster, index)
# identity from observed state. Not an ordering - just a collision breaker.
_LABEL_INDEX = "modelplane.ai/replica-index"


# Scheme for gateway-facing URLs. Traffic between the control plane gateway
# and remote cluster gateways uses plain HTTP; TLS terminates at the edge.
_GATEWAY_SCHEME = "http"


# TODO(https://github.com/crossplane/function-sdk-python/issues/217): Replace
# Resolution and resolve_required with the SDK helper once it lands upstream.
class Resolution(enum.Enum):
    """The state of a required resource the function asked Crossplane to fetch.

    request.get_required_resource returns None both before Crossplane has
    resolved a requirement and after it resolved one to nothing; only the
    requirement key's presence in the request tells them apart (see the
    crossplane.function.request docstring). This makes that distinction
    explicit so callers can treat "not fetched yet" (transient, self-clearing)
    differently from "fetched, doesn't exist" (a deleted resource or a typo).
    """

    UNRESOLVED = "unresolved"  # Crossplane hasn't fetched the requirement yet.
    ABSENT = "absent"  # Resolved, but no resource matched.
    PRESENT = "present"  # Resolved and found.


def resolve_required(req, name: str) -> tuple[Resolution, dict | None]:
    """Classify a required resource into a Resolution and its value.

    Returns (PRESENT, resource) when one was found, (ABSENT, None) when the
    requirement resolved but matched nothing, and (UNRESOLVED, None) when
    Crossplane hasn't fetched it yet. The caller declares the requirement with
    response.require_resources; this only reads the result back.
    """
    if name not in req.required_resources:
        return Resolution.UNRESOLVED, None
    got = request.get_required_resource(req, name)
    return (Resolution.PRESENT, got) if got is not None else (Resolution.ABSENT, None)


def _inference_cluster(
    ic: icv1alpha1.InferenceCluster,
) -> icv1alpha1.InferenceCluster:
    """Return a copy with status fields defaulted."""
    ic = ic.model_copy(deep=True)
    ic.status = ic.status or icv1alpha1.Status()
    ic.status.providerConfigRef = ic.status.providerConfigRef or icv1alpha1.ProviderConfigRef()
    ic.status.gateway = ic.status.gateway or icv1alpha1.Gateway()
    ic.status.gpuPools = ic.status.gpuPools or []
    return ic


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelDeployment(**resource.struct_to_dict(req.observed.composite.resource))
        # The deployment's name and namespace are always set on the observed
        # composite read from the API server.
        assert self.xr.metadata is not None and self.xr.metadata.name is not None
        self.name: str = self.xr.metadata.name

        # Required resources — set by resolve_inputs.
        self.clusters = []
        self.all_replicas = []

        # Whether the scheduler may place NEW replicas this reconcile. Cleared
        # when a referenced ModelCache can't be resolved: we keep retained
        # replicas but don't spread new ones onto clusters that might be outside
        # the (then-unknown) cache footprint. Set by resolve_inputs.
        self.fill = True

    def compose(self):
        if not self.resolve_inputs():
            return
        try:
            matched = self.schedule()
        except cel.CELCompileError as e:
            # A malformed nodeSelector CEL selector is a user error. Surface it
            # and stop - we can't schedule without valid selectors.
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_REPLICAS_SCHEDULED,
                    status="False",
                    reason=CONDITION_REASON_INVALID_NODE_SELECTOR,
                    message=f"Invalid nodeSelector CEL expression: {e}",
                ),
            )
            response.warning(self.rsp, f"Invalid nodeSelector CEL expression: {e}")
            self.rsp.desired.composite.ready = fnv1.READY_FALSE
            return
        self.compose_replicas(matched)
        self.compose_endpoints(matched)
        self.write_status(matched)
        self.derive_conditions(matched)

    def resolve_inputs(self):
        """Declare and fetch required resources. Returns False if critical
        inputs are missing."""
        # Candidate clusters are those matching the deployment's own
        # clusterSelector, narrowed further to where the referenced ModelCache
        # stages its weights. Both selectors are matchLabels (AND semantics), so
        # the candidate set is their intersection. Constraining placement to the
        # cache's footprint keeps a replica from landing on a cluster the cache
        # never staged to, where its PVC wouldn't exist and the pod would be
        # stuck on a volume mount (#186).
        clusters_match_labels = None
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            clusters_match_labels = dict(self.xr.spec.clusterSelector.matchLabels)

        if self.xr.spec.modelCacheRef:
            resolution, cache_match_labels = self.resolve_cache_footprint(self.xr.spec.modelCacheRef)
            if resolution is Resolution.PRESENT:
                # The cache resolved, so its footprint is known; merge its labels
                # so new replicas land only where it stages. Empty labels (the
                # cache stages everywhere) add no constraint.
                if cache_match_labels:
                    clusters_match_labels = {**(clusters_match_labels or {}), **cache_match_labels}
            else:
                # A referenced cache we couldn't read leaves the footprint
                # unknown. Keep retained replicas where they are (retain ignores
                # fill) but don't place new ones onto clusters the cache might
                # not stage to; we'd risk pinning a replica outside the footprint
                # that retain then holds. resolve_cache_footprint surfaced why;
                # placement resumes once the cache resolves.
                self.fill = False

        # require_resources with no match field matches every resource of the
        # kind, so an unset selector leaves all clusters as candidates.
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_labels=clusters_match_labels,
        )
        # Match all ModelReplicas (across all deployments) so the scheduler can
        # account for capacity already consumed by other deployments.
        response.require_resources(
            self.rsp,
            name="all-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
        )

        cluster_dicts = request.get_required_resources(self.req, "clusters")
        replica_dicts = request.get_required_resources(self.req, "all-replicas")

        if not cluster_dicts:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_REPLICAS_SCHEDULED,
                    status="False",
                    reason=CONDITION_REASON_NO_CLUSTERS,
                ),
            )
            response.warning(self.rsp, "No InferenceClusters found")
            return False

        self.clusters = [_inference_cluster(icv1alpha1.InferenceCluster.model_validate(c)) for c in cluster_dicts]
        self.all_replicas = [mrv1alpha1.ModelReplica.model_validate(r) for r in replica_dicts]

        return True

    def resolve_cache_footprint(self, ref) -> tuple[Resolution, dict[str, str] | None]:
        """Resolve the cluster footprint of the ModelCache named by ref.

        Returns a (Resolution, matchLabels) pair:

        * PRESENT - the referenced cache was read. matchLabels is its
          clusterSelector labels, possibly empty (the cache stages everywhere).
          New replicas may be placed.
        * UNRESOLVED / ABSENT - the cache couldn't be read, either because
          Crossplane hasn't fetched it yet (UNRESOLVED) or because it doesn't
          exist (ABSENT). matchLabels is None and the footprint is unknown, so
          the caller holds off placing new replicas.

        Requires the cache by name in the deployment's own namespace, since
        modelCacheRef points at a cache there, and sets the ModelCacheResolved
        condition describing the outcome.
        """
        response.require_resources(
            self.rsp,
            name="cache",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelCache",
            match_name=ref.name,
            namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
        )

        resolution, cache_dict = resolve_required(self.req, "cache")
        if resolution is Resolution.UNRESOLVED:
            # Transient: Crossplane will re-call once it fetches the cache. A
            # condition records the wait; no event, since it self-clears and one
            # every reconcile would just be noise.
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_CACHE_RESOLVED,
                    status="False",
                    reason=CONDITION_REASON_CACHE_UNRESOLVED,
                    message=f"Waiting for ModelCache {ref.name}",
                ),
            )
            return resolution, None

        if resolution is Resolution.ABSENT:
            # The cache doesn't exist - a deleted cache, or a typo in the ref.
            # User-actionable and persistent, so warn as well as condition.
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_CACHE_RESOLVED,
                    status="False",
                    reason=CONDITION_REASON_CACHE_NOT_FOUND,
                    message=f"ModelCache {ref.name} not found; holding replica placement",
                ),
            )
            response.warning(self.rsp, f"ModelCache {ref.name} not found; holding replica placement")
            return resolution, None

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_CACHE_RESOLVED,
                status="True",
                reason=CONDITION_REASON_CACHE_RESOLVED,
            ),
        )

        cache = mcv1alpha1.ModelCache.model_validate(cache_dict)
        if cache.spec.clusterSelector and cache.spec.clusterSelector.matchLabels:
            return resolution, dict(cache.spec.clusterSelector.matchLabels)

        # A cache with no selector stages to every cluster, so it adds no
        # constraint beyond the deployment's own selector.
        return resolution, {}

    def schedule(self):
        """Match the deployment's engines against available clusters."""
        matched = scheduling.schedule(self.xr, self.clusters, self.all_replicas, fill=self.fill)

        # Transition: emit which clusters were matched (first time only). A
        # cluster can host several replicas, so report it once.
        if not matched:
            return matched
        prev_replica_count = sum(1 for c in matched if name.replica_key(c) in self.req.observed.resources)
        if prev_replica_count == 0:
            matched_names = sorted({c.name for c in matched})
            response.normal(
                self.rsp,
                f"Scheduled {len(matched)} replicas across {len(matched_names)} clusters: {', '.join(matched_names)}",
            )

        return matched

    def compose_replicas(self, matched):
        """Compose a ModelReplica per matched cluster.

        Each replica mirrors the deployment's engines with the scheduler's
        per-engine placement resolved onto them, and is pinned to a specific
        cluster via spec.clusterName. Once composed, the pin is stable - the
        scheduler retains the assignment across reconciles. See
        scheduling.schedule for the retain-then-place logic.
        """
        # Index the deployment's engines by name so each placement (keyed by
        # engine name) can be joined with its template and member shape.
        engines_by_name = {e.name: e for e in self.xr.spec.engines}

        for cluster_info in matched:
            replica_key = name.replica_key(cluster_info)

            replica = mrv1alpha1.ModelReplica(
                metadata=metav1.ObjectMeta(
                    name=name.replica(self.name, cluster_info),
                    namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
                    labels={
                        _LABEL_DEPLOYMENT: self.name,
                        _LABEL_CLUSTER: cluster_info.name,
                        _LABEL_INDEX: str(cluster_info.index),
                    },
                ),
                spec=mrv1alpha1.SpecModel(
                    clusterName=cluster_info.name,
                    engines=[self._replica_engine(engines_by_name[ep.name], ep) for ep in cluster_info.engines],
                ),
            )
            if self.xr.spec.modelCacheRef:
                replica.spec.modelCacheRef = mrv1alpha1.ModelCacheRef(name=self.xr.spec.modelCacheRef.name)
            # The replica backend reads serving to pick unified vs. disaggregated
            # routing; copy it down unchanged.
            if self.xr.spec.serving:
                replica.spec.serving = mrv1alpha1.Serving.model_validate(
                    self.xr.spec.serving.model_dump(exclude_unset=True)
                )
            resource.update(self.rsp.desired.resources[replica_key], replica)

    def _replica_engine(self, engine, placement):
        """Build a ModelReplica engine from a deployment engine + placement.

        The engine keeps its name, copies, phase, and member templates verbatim;
        the scheduler supplies each member's pool (nodePoolName) and resolved
        claim: DRA device requests. A member that claims nothing - no
        nodeSelector, or only synthetic devices matched - carries only its pool
        pin; the scheduler guarantees at least one member of every engine
        carries requests.
        """
        members = []
        for member, mp in zip(engine.members, placement.members, strict=True):
            replica_member = mrv1alpha1.Member(
                role=member.role,
                nodePoolName=mp.pool,
                template=mrv1alpha1.Template.model_validate(member.template.model_dump(exclude_unset=True)),
            )
            # A claimless member omits deviceRequests rather than carrying an
            # empty list; setting [] would serialize a literal empty array into
            # the composed manifest.
            if mp.device_requests:
                replica_member.deviceRequests = [
                    mrv1alpha1.DeviceRequest(
                        name=r.name,
                        deviceClassName=r.device_class_name,
                        count=r.count,
                        selectors=[mrv1alpha1.Selector(cel=c) for c in r.cel_selectors],
                    )
                    for r in mp.device_requests
                ]
            # Only a Worker carries the worker block, and only when the user
            # set one. Setting worker=None explicitly would serialize a literal
            # null into the composed manifest rather than omitting the field.
            if member.worker is not None:
                replica_member.worker = mrv1alpha1.Worker(nodes=member.worker.nodes)
            members.append(replica_member)
        replica_engine = mrv1alpha1.Engine(
            name=engine.name,
            copies=engine.copies,
            members=members,
        )
        # phase is only set under PrefillDecode serving; omit it otherwise so it
        # doesn't serialize a null into the composed ModelReplica.
        if engine.phase is not None:
            replica_engine.phase = engine.phase
        return replica_engine

    def compose_endpoints(self, matched):
        """Compose one ModelEndpoint per matched replica.

        Endpoints are labeled with the deployment name so a ModelService
        can select them. The URL points at the per-replica path on the
        remote cluster's gateway. The rewritePath tells ModelService what
        URL prefix to rewrite to on the remote cluster. The path is
        per-replica — /<namespace>/<replica-name>/ — matching the HTTPRoute
        emitted by compose-model-replica's backends (named after the replica
        so co-located replicas on one cluster don't collide).

        Replicas pinned to clusters that are currently unavailable (no
        gateway address) get no endpoint. Routing must not direct
        traffic at a dead backend. When the cluster recovers and its
        gateway address is observed again the endpoint will be composed
        on the next reconcile.

        For the same reason an endpoint is withheld until its ModelReplica
        is Ready. The replica's Ready tracks both the engine workloads
        serving and the remote Service and HTTPRoute that front them - the
        whole traffic path the endpoint advertises. Composing the endpoint
        any earlier routes traffic at pods still pulling images or loading
        weights, returning 503s during deployment and scale-up (#102). The
        endpoint is composed on the reconcile that first observes the
        replica Ready, and withdrawn again if the replica later goes
        not-Ready, pulling a dead backend out of rotation.
        """
        for cluster_info in matched:
            if not cluster_info.gateway_address:
                continue

            replica_observed = self.req.observed.resources.get(name.replica_key(cluster_info))
            if resource.get_condition(replica_observed, "Ready").status != "True":
                continue

            # The replica name (== the ModelReplica and the backend's workload
            # resources) is the per-placement routing key. Must match the name
            # composed in compose_replicas so routing lands on this replica.
            replica_name = name.replica(self.name, cluster_info)
            rewrite_path = f"/{self.xr.metadata.namespace}/{replica_name}/"  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
            endpoint_key = name.endpoint_key(cluster_info)
            url = f"{_GATEWAY_SCHEME}://{cluster_info.gateway_address}{rewrite_path}v1"

            resource.update(
                self.rsp.desired.resources[endpoint_key],
                mev1alpha1.ModelEndpoint(
                    metadata=metav1.ObjectMeta(
                        name=replica_name,
                        namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
                        labels={
                            _LABEL_DEPLOYMENT: self.name,
                            _LABEL_CLUSTER: cluster_info.name,
                            _LABEL_INDEX: str(cluster_info.index),
                        },
                    ),
                    spec=mev1alpha1.Spec(
                        url=url,
                        rewritePath=rewrite_path,
                    ),
                ),
            )

    def write_status(self, matched):
        """Write deployment status: replica counts."""
        replicas_ready = sum(
            1
            for c in matched
            if resource.get_condition(self.req.observed.resources.get(name.replica_key(c)), "Ready").status == "True"
        )

        status = v1alpha1.Status(
            replicas=v1alpha1.Replicas(total=len(matched), ready=replicas_ready),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, matched):
        """Derive ReplicasScheduled and ReplicasReady. Per-resource
        readiness is marked here too."""
        self.derive_replicas_scheduled(matched)
        self.derive_replicas_ready(matched)
        self.mark_endpoint_readiness(matched)

        # When no replicas are scheduled, explicitly mark not ready. Without
        # this, an XR with no composed resources would be trivially ready.
        if not matched:
            self.rsp.desired.composite.ready = fnv1.READY_FALSE

    def derive_replicas_scheduled(self, matched):
        """ReplicasScheduled: replicas placed and created."""
        any_observed = any(name.replica_key(c) in self.req.observed.resources for c in matched)
        scheduled = len(matched) > 0 and any_observed
        desired = int(self.xr.spec.replicas)

        if not matched:
            reason = CONDITION_REASON_INSUFFICIENT_CAPACITY
            msg = f"0 of {desired} replicas scheduled (checked {len(self.clusters)} clusters)"
        elif scheduled:
            reason = CONDITION_REASON_REPLICAS_CREATED
            msg = f"Scheduled {len(matched)} of {desired} replicas"
        else:
            reason = CONDITION_REASON_SCHEDULING
            msg = ""

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_REPLICAS_SCHEDULED,
                status="True" if scheduled else "False",
                reason=reason,
                message=msg,
            ),
        )

    def derive_replicas_ready(self, matched):
        """ReplicasReady: all replicas are serving traffic."""
        replicas_ready = 0
        for c in matched:
            replica_key = name.replica_key(c)
            if resource.get_condition(self.req.observed.resources.get(replica_key), "Ready").status == "True":
                self.rsp.desired.resources[replica_key].ready = fnv1.READY_TRUE
                replicas_ready += 1

        all_ready = len(matched) > 0 and replicas_ready == len(matched)

        if not matched:
            reason = CONDITION_REASON_NO_REPLICAS_SCHEDULED
            msg = ""
        elif all_ready:
            reason = CONDITION_REASON_ALL_REPLICAS_READY
            msg = f"{replicas_ready} of {len(matched)} ready"
        else:
            reason = CONDITION_REASON_MODEL_STARTING
            msg = f"{replicas_ready} of {len(matched)} ready"

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_REPLICAS_READY,
                status="True" if all_ready else "False",
                reason=reason,
                message=msg,
            ),
        )

    def mark_endpoint_readiness(self, matched):
        """Mark each composed ModelEndpoint Ready when observed Ready."""
        for c in matched:
            endpoint_key = name.endpoint_key(c)
            if endpoint_key not in self.rsp.desired.resources:
                continue
            if resource.get_condition(self.req.observed.resources.get(endpoint_key), "Ready").status == "True":
                self.rsp.desired.resources[endpoint_key].ready = fnv1.READY_TRUE
