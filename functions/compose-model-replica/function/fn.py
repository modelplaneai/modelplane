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

"""Deploy a model on a single InferenceCluster.

This function reads the referenced InferenceCluster via required resources, then
composes the cluster-level serving resources for each of the replica's worker
engines. An engine's member roles select its backend: a Standalone member composes
to a Deployment (native), a Leader plus Worker to the cluster's chosen multi-node
backend - a LeaderWorkerSet (llm-d) or a PodCliqueSet (Grove), per the
InferenceCluster's stack. One HTTPRoute, InferencePool and endpoint picker
front all of a replica's engines.

Each member's template is a curated subset of PodTemplateSpec. The container
named "engine" is the inference engine; its image, command, and args are passed
through verbatim to the composed workload - Modelplane injects no engine flags.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function import cache, routing
from function.backends import base, grove, llmd, native

# Condition types and reasons for the ModelReplica XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_SERVING = "ModelServing"
CONDITION_TYPE_MODEL_READY = "ModelReady"

CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_CACHE = "WaitingForCache"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_DEGRADED = "Degraded"
CONDITION_REASON_WAITING_FOR_ROUTING = "WaitingForRouting"
CONDITION_REASON_ROLLING_OUT = "RollingOut"
CONDITION_REASON_ALL_COPIES_READY = "AllCopiesReady"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Backend registry: an engine's member roles select which one composes its
# workload.
_BACKENDS = {
    base.NATIVE: native.NativeBackend,
    base.LLMD: llmd.LLMDBackend,
    base.GROVE: grove.GroveBackend,
}


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
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
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelReplica(**resource.struct_to_dict(req.observed.composite.resource))
        self.ic = None

    def compose(self) -> None:
        if not self.resolve_inputs():
            return
        found, mount = self.resolve_cache()
        if not found:
            return
        self.compose_model_serving(mount)
        self.derive_conditions()

    def resolve_cache(self) -> tuple[bool, mcv1alpha1.Mount | None]:
        """How the replica's engines read its ModelCache, if it reads one.

        Returns (True, mount) once the cache publishes a mount for the
        replica's cluster, or (True, None) when the replica reads no cache.
        A new replica waits for the mount, and composes nothing until then, so
        its engines don't start before the model is there. A replica whose
        engines already run keeps the cache volume, mount and env their
        workload has now, or no cache if it has none, until the cache
        publishes a mount. So a cache that stops being Ready, is deleted, or
        is added or switched to before it's Ready doesn't roll a serving
        model's engines. Returns (False, None) while waiting.
        """
        resolution = cache.resolve(self.req, self.xr)
        if resolution is None:
            return True, None
        if resolution.mount is not None:
            return True, resolution.mount
        keys = base.workload_keys(self.xr)
        if any(k in self.req.observed.resources for k in keys):
            kept = cache.kept(self.xr, self.req.observed.resources, keys)
            response.warning(self.rsp, kept.waiting)
            return True, kept.mount
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_ACCEPTED,
                status="False",
                reason=CONDITION_REASON_WAITING_FOR_CACHE,
                message=resolution.waiting,
            ),
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_SERVING, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
            ),
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
            ),
        )
        # Nothing is composed while waiting, and an XR with no composed
        # resources would otherwise be trivially ready.
        self.rsp.desired.composite.ready = fnv1.READY_FALSE
        return False, None

    def resolve_inputs(self) -> bool:
        """Declare the referenced InferenceCluster and ModelCache, and fetch the cluster."""
        cache.require(self.rsp, self.xr)
        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=self.xr.spec.clusterName,
        )

        ic_dict = request.get_required_resource(self.req, "cluster")
        if ic_dict is None:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_ACCEPTED, status="False", reason=CONDITION_REASON_WAITING_FOR_CLUSTER
                ),
            )
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_SERVING, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster to be resolved")
            # Nothing is composed while waiting, and an XR with no composed
            # resources would otherwise be trivially ready.
            self.rsp.desired.composite.ready = fnv1.READY_FALSE
            return False

        self.ic = icv1alpha1.InferenceCluster.model_validate(ic_dict)

        if not (self.ic.status and self.ic.status.providerConfigRef and self.ic.status.providerConfigRef.name):
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_ACCEPTED, status="False", reason=CONDITION_REASON_WAITING_FOR_CLUSTER
                ),
            )
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_SERVING, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster providerConfigRef")
            self.rsp.desired.composite.ready = fnv1.READY_FALSE
            return False

        return True

    def compose_model_serving(self, mount: mcv1alpha1.Mount | None) -> None:
        """Compose each engine's workload, then the replica's routing surface.

        Every engine composes to a Deployment, LeaderWorkerSet, or PodCliqueSet (with its
        members' ResourceClaimTemplates) via the backend its roles select; the
        backends build no routing. routing.apply then fronts the engines with an
        InferencePool and endpoint picker, whose scoring serving.mode selects.
        Everything lands in the namespace mirroring the replica's own. A
        PrefillDecode replica that lacks its routing gets a fatal result and
        composes nothing.
        """
        # resolve_inputs runs first and returns False unless the cluster's
        # status.providerConfigRef.name is set, so it's present here.
        assert self.ic and self.ic.status and self.ic.status.providerConfigRef and self.ic.status.providerConfigRef.name
        pc = self.ic.status.providerConfigRef.name
        # The XRD defaults stack, so it's always set; a gang engine composes to
        # its value (Standard or Dynamo). Coalesce for the type checker, which
        # sees the field as optional.
        stack = self.ic.spec.stack or "Standard"
        if routing.prefill_decode_routing_missing(self.xr):
            # Stop rather than compose a router the engines can't use. A fatal
            # result leaves the replica's existing resources as they are, so an
            # existing pair keeps serving until its ModelDeployment writes its
            # routing.
            response.fatal(
                self.rsp,
                "PrefillDecode serving requires serving.router: EndpointPicker and serving.endpointPicker",
            )
            return
        if routing.decode_without_sidecar(self.xr):
            response.warning(
                self.rsp,
                "The Decode engine's serving pod has no container beside the engine. llm-d's picker sends "
                "prefill through a sidecar in the decode pod, so without one every request skips prefill.",
            )
        label = base.serving_label(self.xr)
        composed: dict[str, k8sobjv1alpha1.Object] = {}
        for engine in self.xr.spec.engines:
            backend = _BACKENDS[base.select_backend(engine, stack)]()
            composed.update(backend.build(self.xr, engine, pc, label, stack, mount))
        composed = routing.apply(composed, self.xr, pc)
        for key, obj in composed.items():
            resource.update(self.rsp.desired.resources[key], obj)
        # First-time transition: emit a normal event the first reconcile, before
        # any workload is observed.
        if not any(k in self.req.observed.resources for k in base.workload_keys(self.xr)):
            image = base.engine_container(self.xr.spec.engines[0].members[0]).image
            response.normal(self.rsp, f"Composing {image} on {self.xr.spec.clusterName}")

    def derive_conditions(self) -> None:
        """Derive ModelAccepted, ModelServing and ModelReady.

        Each implies the one before it, so the first False one says what the
        replica is waiting on. ModelAccepted is True once every engine's
        workload exists on the cluster. ModelServing is True once at least one
        copy of every engine is ready and the routing in front of them is in
        place, so the replica can take traffic. ModelReady is True once every
        copy is ready, so the replica is at full capacity; the replica's Ready
        mirrors it.
        """
        engines = self.xr.spec.engines
        observed = self.req.observed.resources

        def ready(key: str) -> bool:
            return resource.get_condition(observed.get(key), "Ready").status == "True"

        # The PrefillDecode routing check stops composing with a fatal result,
        # which leaves nothing desired. Crossplane keeps the existing resources
        # then, so judge the replica on what it observes.
        keys = set(self.rsp.desired.resources) or set(observed)
        workloads = {base.workload_key(e) for e in engines} | {base.scaling_group_key(e) for e in engines}
        # The endpoint picker, the routing in front of it, and the engines'
        # claim templates.
        others_ready = all(ready(k) for k in keys - workloads)

        # resolve_inputs ran first, so the cluster is resolved.
        assert self.ic
        stack = self.ic.spec.stack or "Standard"
        copies = {e.name: int(e.copies or 1) for e in engines}
        ready_copies = {
            e.name: min(_BACKENDS[base.select_backend(e, stack)]().ready_copies(e, observed), copies[e.name])
            for e in engines
        }

        accepted = all(self._workload_accepted(base.workload_key(e)) for e in engines)
        copy_each = all(ready_copies[name] > 0 for name in copies)
        serving = accepted and others_ready and copy_each
        full = serving and all(ready(k) for k in keys)

        if serving and sum(ready_copies.values()) < sum(copies.values()):
            waiting = CONDITION_REASON_DEGRADED
        elif serving:
            # Every copy counts as ready but a workload doesn't yet, as during
            # a LeaderWorkerSet's rolling update.
            waiting = CONDITION_REASON_ROLLING_OUT
        elif accepted and copy_each:
            waiting = CONDITION_REASON_WAITING_FOR_ROUTING
        elif accepted:
            waiting = CONDITION_REASON_MODEL_STARTING
        else:
            waiting = CONDITION_REASON_WAITING_FOR_MODEL
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_ACCEPTED,
                status="True" if accepted else "False",
                reason=CONDITION_REASON_ACCEPTED if accepted else CONDITION_REASON_DEPLOYING,
            ),
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_SERVING,
                status="True" if serving else "False",
                reason=CONDITION_REASON_SERVING if serving else waiting,
            ),
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_READY,
                status="True" if full else "False",
                reason=CONDITION_REASON_ALL_COPIES_READY if full else waiting,
                message=f"{sum(ready_copies.values())} of {sum(copies.values())} engine copies ready",
            ),
        )

        # Per-resource readiness. Crossplane gates the XR's Ready on every
        # composed resource being ready, but doesn't read a composed resource's
        # own Ready condition, so the function relays it. Each Object's
        # readiness policy decides that condition: a workload once every copy is
        # ready, the endpoint picker once its CEL query sees it available,
        # anything else once applied. Envoy AI Gateway fails closed without a
        # picker, whatever the pool's failureMode says, so the replica can't
        # serve until its picker is available.
        for key in self.rsp.desired.resources:
            if ready(key):
                self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def _workload_accepted(self, key: str) -> bool:
        """Whether a workload Object has been created on the remote cluster.

        True once provider-kubernetes populates the Object's
        atProvider.manifest, which it does after observing the remote resource
        at least once.
        """
        observed = self.req.observed.resources.get(key)
        if not observed:
            return False
        obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
        return bool(obj.status and obj.status.atProvider and obj.status.atProvider.manifest)
