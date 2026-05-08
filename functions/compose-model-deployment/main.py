"""Schedule a ModelDeployment across the InferenceCluster fleet.

This function discovers the fleet, runs the federation scheduler over
declared substrate, and composes one ModelReplica per logical replica plus
one ModelEndpoint per replica. The scheduling algorithm itself is in
scheduling.py; this file is the Crossplane glue (required-resources, status,
conditions, events).
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import adapters, emitters, scheduling
from .lib import conditions, defaults, metadata, naming
from .lib import resource as libresource

# When #64 lands these become real:
#   from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
#   from .model.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1


# Condition types and reasons for the ModelDeployment XR.
CONDITION_TYPE_SCHEDULED = "Scheduled"
CONDITION_TYPE_REPLICAS_READY = "ReplicasReady"

CONDITION_REASON_NO_CLUSTERS = "NoEligibleClusters"
CONDITION_REASON_FLEET_SATURATED = "FleetSaturated"
CONDITION_REASON_CONFIG_INVALID = "ConfigInvalid"
CONDITION_REASON_PARTIALLY_SCHEDULED = "PartiallyScheduled"
CONDITION_REASON_FULLY_SCHEDULED = "AllReplicasScheduled"
CONDITION_REASON_ZERO_REPLICAS = "ZeroReplicas"
CONDITION_REASON_REPLICAS_PROGRESSING = "ReplicasProgressing"
CONDITION_REASON_REPLICAS_READY = "AllReplicasReady"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = adapters.load_md(req)

        # Required resources — set by resolve_inputs.
        self.clusters: list[scheduling.InferenceCluster] = []
        self.existing: list[scheduling.ExistingPlacement] = []

    def compose(self):
        if not self.resolve_inputs():
            return
        result = self.schedule()
        self.compose_replicas(result)
        self.compose_endpoints(result)
        self.write_status(result)
        self.derive_conditions(result)

    def resolve_inputs(self) -> bool:
        """Declare and fetch required resources. Returns False if critical
        inputs are missing."""
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        response.require_resources(
            self.rsp,
            name="classes",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceClass",
        )
        # Only fetch ModelReplicas owned by this MD — emitters set this label.
        response.require_resources(
            self.rsp,
            name="existing-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
            match_labels={metadata.LABEL_KEY_DEPLOYMENT: self.xr.name},
        )

        cluster_dicts = request.get_required_resources(self.req, "clusters")
        class_dicts = request.get_required_resources(self.req, "classes")
        replica_dicts = request.get_required_resources(self.req, "existing-replicas")

        if cluster_dicts is None or class_dicts is None or replica_dicts is None:
            return False  # waiting on Crossplane to fetch

        if not cluster_dicts:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_SCHEDULED,
                False,
                CONDITION_REASON_NO_CLUSTERS,
            )
            response.warning(self.rsp, "No InferenceClusters in the fleet")
            return False

        try:
            self.clusters = adapters.resolve_clusters(cluster_dicts, class_dicts)
            self.existing = adapters.resolve_existing(replica_dicts)
        except (ValueError, KeyError) as e:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_SCHEDULED,
                False,
                CONDITION_REASON_CONFIG_INVALID,
                str(e),
            )
            response.warning(self.rsp, f"Cluster fleet config invalid: {e}")
            return False

        return True

    def schedule(self) -> scheduling.ScheduleResult:
        """Run the federation scheduler over the fleet. Filter → Score → Bind."""
        result = scheduling.schedule(self.xr, self.clusters, self.existing)

        # Emit on first successful placement of each replica (transition).
        new_placements = [
            p for p in result.placements
            if f"replica-{p.replica_index}" not in self.req.observed.resources
        ]
        if new_placements:
            targets = [f"r{p.replica_index}→{p.cluster}/{p.decode.pool}" for p in new_placements]
            response.normal(self.rsp, f"Scheduled {len(new_placements)} replica(s): {', '.join(targets)}")

        return result

    def compose_replicas(self, result: scheduling.ScheduleResult):
        """Compose a ModelReplica per Placement."""
        for p in result.placements:
            key = f"replica-{p.replica_index}"
            resource.update(
                self.rsp.desired.resources[key],
                emitters.build_replica(self.xr, p),
            )

    def compose_endpoints(self, result: scheduling.ScheduleResult):
        """Compose a ModelEndpoint per ModelReplica (per Nic's #64)."""
        for p in result.placements:
            key = f"endpoint-{p.replica_index}"
            resource.update(
                self.rsp.desired.resources[key],
                emitters.build_endpoint(self.xr, p),
            )

    def write_status(self, result: scheduling.ScheduleResult):
        """Write deployment status: replica counts, matchTrace."""
        replicas_ready = sum(
            1 for p in result.placements
            if conditions.has_condition(self.req, f"replica-{p.replica_index}", "Ready")
        )
        status = {
            "modelReplicas": {
                "total": len(result.placements),
                "ready": replicas_ready,
            },
            "matchTrace": emitters.build_match_trace(result.trace),
        }
        # When pydantic models exist post-#64:
        #   status = mdv1alpha1.Status(modelReplicas=..., matchTrace=...)
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, result: scheduling.ScheduleResult):
        """Derive Scheduled and ReplicasReady conditions."""
        self.derive_scheduled(result)
        self.derive_replicas_ready(result)

        # Without any placements the XR has no composed resources backing it;
        # explicitly mark not ready so downstream watchers don't treat empty
        # as ready.
        if not result.placements and self.xr.replicas > 0:
            self.rsp.desired.composite.ready = fnv1.READY_FALSE

    def derive_scheduled(self, result: scheduling.ScheduleResult):
        """Scheduled: every replica has a (cluster, pool) binding."""
        if self.xr.replicas == 0:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_SCHEDULED,
                True,
                CONDITION_REASON_ZERO_REPLICAS,
            )
            return

        if not result.placements:
            reason = (
                CONDITION_REASON_FLEET_SATURATED
                if all(t.reason == "capacity" for t in result.trace)
                else CONDITION_REASON_NO_CLUSTERS
            )
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_SCHEDULED,
                False,
                reason,
                _trace_summary(result.trace),
            )
            return

        if len(result.placements) < self.xr.replicas:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_SCHEDULED,
                False,
                CONDITION_REASON_PARTIALLY_SCHEDULED,
                f"{len(result.placements)}/{self.xr.replicas} replicas scheduled. "
                + _trace_summary(result.trace),
            )
            return

        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_SCHEDULED,
            True,
            CONDITION_REASON_FULLY_SCHEDULED,
        )

    def derive_replicas_ready(self, result: scheduling.ScheduleResult):
        """ReplicasReady: derived from observed child ModelReplica conditions."""
        if not result.placements:
            return  # no children to be ready
        all_ready = all(
            conditions.has_condition(self.req, f"replica-{p.replica_index}", "Ready")
            for p in result.placements
        )
        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_REPLICAS_READY,
            all_ready,
            CONDITION_REASON_REPLICAS_READY if all_ready else CONDITION_REASON_REPLICAS_PROGRESSING,
        )


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    rsp = response.to(req)
    Composer(req, rsp).compose()
    return rsp


def _trace_summary(trace: list[scheduling.MatchTrace]) -> str:
    """Compact one-line summary of why scheduling failed.

    The full structured trace lands on MD.status.matchTrace via emitters.
    This is the human-readable headline for the condition message.
    """
    if not trace:
        return "no eligible InferenceClusters in the fleet"
    by_reason: dict[str, int] = {}
    for t in trace:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    return "; ".join(f"{r}: {n}" for r, n in by_reason.items())


# Silence unused-import warnings for the symbols above that real adapters
# will use once #64's protos are generated.
_ = (defaults, naming)
