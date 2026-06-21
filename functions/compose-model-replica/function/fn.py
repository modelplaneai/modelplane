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
to a Deployment (native), a Leader plus Worker to a LeaderWorkerSet (llm-d). One
shared Service and HTTPRoute front all of a replica's engines.

Each member's template is a curated subset of PodTemplateSpec. The container
named "engine" is the inference engine; its image, command, and args are passed
through verbatim to the composed workload - Modelplane injects no engine flags.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function import routing
from function.backends import base, dynamo, llmd, native

# Condition types and reasons for the ModelReplica XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_READY = "ModelReady"

CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Backend registry: an engine's member roles select which one composes its
# workload.
_BACKENDS = {
    base.NATIVE: native.NativeBackend,
    base.LLMD: llmd.LLMDBackend,
    base.DYNAMO: dynamo.DynamoBackend,
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
    def __init__(self, req, rsp) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelReplica(**resource.struct_to_dict(req.observed.composite.resource))
        self.ic = None

    def compose(self) -> None:
        if not self.resolve_inputs():
            return
        self.compose_model_serving()
        self.derive_conditions()

    def resolve_inputs(self) -> bool:
        """Declare and fetch the referenced InferenceCluster."""
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
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster to be resolved")
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
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster providerConfigRef")
            return False

        return True

    def compose_model_serving(self) -> None:
        """Compose each engine's workload, then the replica's routing surface.

        Every engine composes to a Deployment or LeaderWorkerSet (with its
        members' ResourceClaimTemplates) via the backend its roles select; the
        backends build no routing. routing.apply then fronts the engines with the
        surface serving.mode selects: a Service (Unified) or an InferencePool +
        endpoint picker (PrefillDecode).
        """
        # resolve_inputs runs first and returns False unless the cluster's
        # status.providerConfigRef.name is set, so it's present here.
        assert self.ic and self.ic.status and self.ic.status.providerConfigRef and self.ic.status.providerConfigRef.name
        pc = self.ic.status.providerConfigRef.name
        label = base.serving_label(self.xr)
        composed: dict[str, k8sobjv1alpha1.Object] = {}
        for engine in self.xr.spec.engines:
            backend = _BACKENDS[base.select_backend(engine)]()
            composed.update(backend.build(self.xr, engine, pc, label))
        composed = routing.apply(composed, self.xr, pc)
        for key, obj in composed.items():
            resource.update(self.rsp.desired.resources[key], obj)

    def derive_conditions(self) -> None:
        """Derive ModelAccepted and ModelReady across all of the replica's engines.

        A replica is accepted when every engine's workload has been created on the
        cluster, and ready when every engine's workload is serving. A
        disaggregated or replicated-engine replica composes several workloads;
        all must be up for the replica to serve.
        """
        workload_keys = base.workload_keys(self.xr)

        # First-time transition: emit a normal event the first reconcile, before
        # any workload is observed.
        if not any(k in self.req.observed.resources for k in workload_keys):
            image = base.engine_container(self.xr.spec.engines[0].members[0]).image
            response.normal(
                self.rsp,
                f"Composing {image} on {self.xr.spec.clusterName}",
            )

        # A workload is accepted once provider-kubernetes populates its Object's
        # atProvider.manifest (it observed the remote resource at least once),
        # and ready once the Object reports Ready=True. The replica is accepted
        # only when every workload is, and ready only when every workload is.
        workload_accepted = all(self._workload_accepted(k) for k in workload_keys)
        workload_ready = all(
            resource.get_condition(self.req.observed.resources.get(k), "Ready").status == "True" for k in workload_keys
        )

        # ModelAccepted: the workloads were created on the cluster.
        accepted_reason = CONDITION_REASON_ACCEPTED if workload_accepted else CONDITION_REASON_DEPLOYING
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_ACCEPTED,
                status="True" if workload_accepted else "False",
                reason=accepted_reason,
            ),
        )

        # ModelReady: the model is actually serving traffic.
        if workload_ready:
            ready_reason = CONDITION_REASON_SERVING
        elif workload_accepted:
            ready_reason = CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_READY, status="True" if workload_ready else "False", reason=ready_reason
            ),
        )

        # Per-resource readiness. Crossplane gates the XR's Ready on every
        # composed resource being ready, so the function must mark each one - a
        # composed resource isn't ready just because provider-kubernetes set its
        # Object's own Ready condition. Marking a resource ready asserts the
        # function observed it ready, so we only ever mark a resource we can see
        # in observed state. A workload additionally gates on the model actually
        # serving; the Service, HTTPRoute, and ResourceClaimTemplates have no
        # runtime readiness to wait on (existing is being ready), so observing
        # them is enough. A freshly composed resource isn't in observed yet, so
        # it stays unready until the next reconcile sees it applied.
        workloads = set(workload_keys)
        for key in self.rsp.desired.resources:
            if key not in self.req.observed.resources:
                continue
            if key in workloads:
                if resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True":
                    self.rsp.desired.resources[key].ready = fnv1.READY_TRUE
            else:
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
