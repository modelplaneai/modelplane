"""Fan out a ModelDeployment to ModelReplicas and ModelEndpoints.

This function discovers InferenceClusters, matches the deployment's
topology against available capacity, creates a ModelReplica per
selected cluster, and creates one ModelEndpoint per replica for
ModelService to route to.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function import cel, scheduling

# Condition types and reasons for the ModelDeployment XR.
CONDITION_TYPE_REPLICAS_SCHEDULED = "ReplicasScheduled"
CONDITION_TYPE_REPLICAS_READY = "ReplicasReady"

CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_INSUFFICIENT_CAPACITY = "InsufficientCapacity"
CONDITION_REASON_INVALID_NODE_SELECTOR = "InvalidNodeSelector"
CONDITION_REASON_REPLICAS_CREATED = "ReplicasCreated"
CONDITION_REASON_SCHEDULING = "Scheduling"
CONDITION_REASON_NO_REPLICAS_SCHEDULED = "NoReplicasScheduled"
CONDITION_REASON_ALL_REPLICAS_READY = "AllReplicasReady"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Label keys and values. These are coordination contracts: compose-model-replica
# and compose-model-service read labels that this function writes.
_LABEL_CLUSTER = "modelplane.ai/cluster"
_LABEL_REPLICA = "modelplane.ai/replica"
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"
# Per-cluster-local index distinguishing co-located replicas of one deployment.
# Read back by the scheduler to reconstruct a replica's (cluster, index)
# identity from observed state. Not an ordering - just a collision breaker.
_LABEL_INDEX = "modelplane.ai/replica-index"
_LABEL_VALUE_TRUE = "true"


def _replica_key(candidate) -> str:
    """Function-local desired-resource handle for a replica's (cluster, index).

    Distinct per co-located replica so two replicas on one cluster don't share a
    desired-resource slot. Deterministic from observed state, so a replica
    placed this reconcile maps back to the same handle once observed.
    """
    return f"replica-{candidate.name}-{candidate.index}"


def _endpoint_key(candidate) -> str:
    """Function-local desired-resource handle for a replica's ModelEndpoint."""
    return f"endpoint-{candidate.name}-{candidate.index}"


def _replica_name(deployment_name: str, candidate) -> str:
    """The opaque, DNS-safe name for a replica's resources.

    Hashed from (deployment, cluster, index) so co-located replicas get distinct
    names. Cluster and index are not exposed in the readable prefix - identity
    lives in labels, the name is just a stable handle (matching Crossplane's
    opaque-name convention, and how a Pod's name doesn't encode its node).
    """
    return resource.child_name(deployment_name, candidate.name, str(candidate.index))


# Scheme for gateway-facing URLs. Traffic between the control plane gateway
# and remote cluster gateways uses plain HTTP; TLS terminates at the edge.
_GATEWAY_SCHEME = "http"


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


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
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

        # Required resources — set by resolve_inputs.
        self.clusters = []
        self.all_replicas = []

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
        # InferenceClusters are matched by the modelplane.ai/cluster=true
        # label — a workaround for the empty match_labels protobuf bug.
        cluster_match_labels: dict[str, str] = {
            _LABEL_CLUSTER: _LABEL_VALUE_TRUE,
        }
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            cluster_match_labels.update(self.xr.spec.clusterSelector.matchLabels)

        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_labels=cluster_match_labels,
        )
        response.require_resources(
            self.rsp,
            name="all-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
            match_labels={_LABEL_REPLICA: _LABEL_VALUE_TRUE},
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

    def schedule(self):
        """Match the deployment's topology against available clusters."""
        matched = scheduling.schedule(self.xr, self.clusters, self.all_replicas)

        # Transition: emit which clusters were matched (first time only). A
        # cluster can host several replicas, so report it once.
        if not matched:
            return matched
        prev_replica_count = sum(1 for c in matched if _replica_key(c) in self.req.observed.resources)
        if prev_replica_count == 0:
            matched_names = sorted({c.name for c in matched})
            response.normal(
                self.rsp,
                f"Scheduled {len(matched)} replicas across {len(matched_names)} clusters: {', '.join(matched_names)}",
            )

        return matched

    def compose_replicas(self, matched):
        """Compose a ModelReplica per matched cluster.

        Each replica inherits the deployment's workers block verbatim
        and is pinned to a specific cluster via spec.clusterName. Once
        composed, the pin is stable - the scheduler retains the
        assignment across reconciles. See scheduling.schedule for the
        retain-then-place logic.
        """
        # Convert via model_dump because the MD and MR Workers types
        # are different Pydantic classes (generated from different XRDs
        # with the same schema).
        workers = mrv1alpha1.Workers.model_validate(self.xr.spec.workers.model_dump(exclude_none=True))

        for cluster_info in matched:
            replica_key = _replica_key(cluster_info)

            # Stamp the resolved claim: DRA device requests so the replica
            # function can form a DRA ResourceClaim. Only set the field when
            # there are requests, so we don't claim ownership of an empty list
            # (only synthetic devices matched) under SSA.
            device_requests = [
                mrv1alpha1.DeviceRequest(
                    name=r.name,
                    deviceClassName=r.device_class_name,
                    count=r.count,
                    selectors=[mrv1alpha1.Selector(cel=c) for c in r.cel_selectors],
                )
                for r in cluster_info.device_requests
            ]

            replica = mrv1alpha1.ModelReplica(
                metadata=metav1.ObjectMeta(
                    name=_replica_name(self.xr.metadata.name, cluster_info),
                    namespace=self.xr.metadata.namespace,
                    labels={
                        _LABEL_REPLICA: _LABEL_VALUE_TRUE,
                        _LABEL_DEPLOYMENT: self.xr.metadata.name,
                        _LABEL_CLUSTER: cluster_info.name,
                        _LABEL_INDEX: str(cluster_info.index),
                    },
                ),
                spec=mrv1alpha1.SpecModel(
                    clusterName=cluster_info.name,
                    workers=workers,
                    # Only set nodePoolName when the scheduler matched a
                    # specific pool. Leaving it unset (rather than null)
                    # avoids claiming ownership of a field we have no
                    # opinion on under server-side apply.
                    **({"nodePoolName": cluster_info.pool} if cluster_info.pool else {}),
                    **({"deviceRequests": device_requests} if device_requests else {}),
                ),
            )
            if self.xr.spec.modelCacheRef:
                replica.spec.modelCacheRef = mrv1alpha1.ModelCacheRef(name=self.xr.spec.modelCacheRef.name)
            resource.update(self.rsp.desired.resources[replica_key], replica)

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
        """
        for cluster_info in matched:
            if not cluster_info.gateway_address:
                continue

            # The replica name (== the ModelReplica and the backend's workload
            # resources) is the per-placement routing key. Must match the name
            # composed in compose_replicas so routing lands on this replica.
            replica_name = _replica_name(self.xr.metadata.name, cluster_info)
            rewrite_path = f"/{self.xr.metadata.namespace}/{replica_name}/"
            endpoint_key = _endpoint_key(cluster_info)
            url = f"{_GATEWAY_SCHEME}://{cluster_info.gateway_address}{rewrite_path}v1"

            resource.update(
                self.rsp.desired.resources[endpoint_key],
                mev1alpha1.ModelEndpoint(
                    metadata=metav1.ObjectMeta(
                        name=replica_name,
                        namespace=self.xr.metadata.namespace,
                        labels={
                            _LABEL_DEPLOYMENT: self.xr.metadata.name,
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
            if resource.get_condition(self.req.observed.resources.get(_replica_key(c)), "Ready").status == "True"
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
        any_observed = any(_replica_key(c) in self.req.observed.resources for c in matched)
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
            replica_key = _replica_key(c)
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
            endpoint_key = _endpoint_key(c)
            if endpoint_key not in self.rsp.desired.resources:
                continue
            if resource.get_condition(self.req.observed.resources.get(endpoint_key), "Ready").status == "True":
                self.rsp.desired.resources[endpoint_key].ready = fnv1.READY_TRUE
