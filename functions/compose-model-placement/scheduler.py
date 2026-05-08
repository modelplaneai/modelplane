"""Per-scheduler wrap functions for the renderer.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS PURE.  No Crossplane / Kubernetes imports. Each wrap
  function is `(llmis_spec, mr_name, namespace, replica_index) ->
  WrappedRender`. Pure dict in / pure dict out.

  Test target: tests/unit/test_scheduler.py
═══════════════════════════════════════════════════════════════════════════

Stage 2 (in-cluster) integration. The matcher (stage 1) is scheduler-
agnostic — it reads `IC.status.capacity`, doesn't care which scheduler
populated it. The renderer is where scheduler choice shows up: how the
rendered LLM-IS / pods get gated for admission and gang scheduling.

Two interception models:

  KAI replaces the K8s scheduler. We set `schedulerName: kai-scheduler`
  on rendered pods and emit a `PodGroup` CRD that wraps the LWS group
  for gang admission. KAI binds pods to nodes itself, evaluating gang
  feasibility, fair-share, MIG fragmentation, NVLink topology in one
  pass.

  Kueue layers above kube-scheduler. We stamp the workload with
  `kueue.x-k8s.io/queue-name` (so Kueue's webhook creates a `Workload`
  CR for it) and gate the rendered Job / Deployment / LWS via
  `spec.suspend: true`. Once the `ClusterQueue` admits, Kueue ungates;
  kube-scheduler binds pods normally. Gang-ness comes from LWS's
  atomic create semantics — no separate gang object.

  none — kube-scheduler best-effort. Pass-through.

Sketch — assumes Nic's #64 protos plus an extension that adds
`InferenceCluster.spec.scheduler.{type}` (auto / managed-kai / managed-kueue
/ kai / kueue / none). The dispatch table at the bottom is the seam any
new scheduler integration plugs into.
"""

from collections.abc import Callable
from dataclasses import dataclass

# When the scheduler axis lands as an XR field these become real:
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1


# ---------------------------------------------------------------------------
# The wrap contract — one function per scheduler.
# ---------------------------------------------------------------------------


@dataclass
class WrappedRender:
    """Result of wrap(). The renderer emits everything in `extra_objects`
    alongside the LLM-IS, in the same target cluster."""

    llmis_spec: dict
    extra_objects: list[dict]  # PodGroup, Workload, etc. — k8s objects to apply


def wrap_kai(llmis_spec: dict, mr_name: str, namespace: str, replica_index: int) -> WrappedRender:
    """KAI: schedulerName + PodGroup.

    Two changes:
      1. Stamp `schedulerName: kai-scheduler` on every pod template the
         LLM-IS produces (workerSpec.containers + prefill.workerSpec if
         disagg). KAI's mutating webhook does this for any pod that forgot,
         but explicit is safer.
      2. Emit a PodGroup CRD that names the gang. Required for multi-pod
         workloads (LWS group); harmless single-pod (`minMember: 1`).
         PodGroup.spec.minMember = total pods in the gang.
    """
    out = _stamp_scheduler_name(llmis_spec, "kai-scheduler")

    gang_size = _gang_size(llmis_spec)
    pod_group = {
        "apiVersion": "scheduling.run.ai/v2alpha2",
        "kind": "PodGroup",
        "metadata": {
            "name": f"{mr_name}-gang",
            "namespace": namespace,
            "labels": {
                # KAI matches pods to a PodGroup by this label.
                "pod-group.scheduling.run.ai/name": f"{mr_name}-gang",
            },
        },
        "spec": {
            "minMember": gang_size,
            "queue": _kai_queue_name(namespace),
            # Priority class — can be tuned per ModelDeployment.spec.priority
            # if we ever expose one. Default is "inference" tier.
            "priorityClassName": "inference",
        },
    }
    # Stamp the matching label on every pod the LLM-IS produces so KAI
    # binds them to the right PodGroup.
    out = _stamp_pod_label(out, "pod-group.scheduling.run.ai/name", f"{mr_name}-gang")

    return WrappedRender(llmis_spec=out, extra_objects=[pod_group])


def wrap_kueue(llmis_spec: dict, mr_name: str, namespace: str, replica_index: int) -> WrappedRender:
    """Kueue: queue label + suspend gate.

    Kueue's webhook creates a `Workload` CR automatically when it sees the
    `kueue.x-k8s.io/queue-name` label on a recognized workload kind. For
    LWS specifically, Kueue v0.8+ has native support — the LWS owner
    creates pods only after Kueue admits.

    Two changes:
      1. Add `kueue.x-k8s.io/queue-name: <queue>` to the LLM-IS top-level
         labels. Kueue's webhook propagates this to the LWS owner
         resource it sees underneath.
      2. Set `suspend: true` on the LWS-owner. Kueue flips it to false
         on admission; pods come up at that moment.

    No separate Workload object — Kueue creates it. We only label and gate.
    """
    queue = _kueue_queue_name(namespace)
    out = dict(llmis_spec)
    out.setdefault("metadata", {}).setdefault("labels", {})[
        "kueue.x-k8s.io/queue-name"
    ] = queue
    # The LLM-IS controller propagates this onto the LWS / Deployment
    # spec.template; Kueue's webhook reads it from the underlying object.

    # Gate via suspend on the rendered worker spec. KServe v0.18 LLM-IS
    # forwards spec.suspend to the LWS owner.
    out["suspend"] = True
    return WrappedRender(llmis_spec=out, extra_objects=[])


def wrap_none(llmis_spec: dict, mr_name: str, namespace: str, replica_index: int) -> WrappedRender:
    """No-op. kube-scheduler best-effort; no admission control; no gang.

    Use case: dev clusters where nothing is installed. Multi-pod workloads
    can still partially admit; that's the operator's choice.
    """
    return WrappedRender(llmis_spec=llmis_spec, extra_objects=[])


# ---------------------------------------------------------------------------
# Dispatch — the seam new schedulers plug into.
# ---------------------------------------------------------------------------


WrapFn = Callable[[dict, str, str, int], WrappedRender]


_DISPATCH: dict[str, WrapFn] = {
    # `auto` is resolved upstream (in the IC onboarding controller) into
    # one of the concrete options before reaching this dispatch. If we see
    # `auto` here, fall back to managed-kueue (safest greenfield default).
    "managed-kai": wrap_kai,
    "kai": wrap_kai,
    "managed-kueue": wrap_kueue,
    "kueue": wrap_kueue,
    "none": wrap_none,
}


def wrap(
    scheduler_type: str,
    llmis_spec: dict,
    mr_name: str,
    namespace: str,
    replica_index: int,
) -> WrappedRender:
    """Top-level dispatch — called from compose-model-placement/main.py.

    Adding a new scheduler (Volcano, etc.):
      1. Implement wrap_<scheduler>() with the same signature.
      2. Add it to _DISPATCH.
      3. Update IC.spec.scheduler.type enum to include it.
      4. Add a capacity adapter under lib/capacity_adapter/.

    No matcher changes. No MD changes. The IR (ModelReplica) doesn't
    know which scheduler is involved — that's entirely renderer concern.
    """
    fn = _DISPATCH.get(scheduler_type, wrap_kueue)  # default: kueue
    return fn(llmis_spec, mr_name, namespace, replica_index)


# ---------------------------------------------------------------------------
# Helpers — pure functions over the LLM-IS spec dict.
# ---------------------------------------------------------------------------


def _stamp_scheduler_name(llmis_spec: dict, name: str) -> dict:
    """Set `schedulerName` on every pod template the LLM-IS produces.

    KServe v0.18 LLM-IS has workerSpec.containers (decode) and optionally
    prefill.workerSpec.containers. The schedulerName is a sibling of
    `containers` on the pod-spec level — KServe propagates it.
    """
    out = dict(llmis_spec)
    if "workerSpec" in out:
        out["workerSpec"] = {**out["workerSpec"], "schedulerName": name}
    if "prefill" in out and out["prefill"] and "workerSpec" in out["prefill"]:
        out["prefill"] = {
            **out["prefill"],
            "workerSpec": {**out["prefill"]["workerSpec"], "schedulerName": name},
        }
    return out


def _stamp_pod_label(llmis_spec: dict, key: str, value: str) -> dict:
    """Add a label that flows through to every pod the LLM-IS produces.

    KServe propagates labels under workerSpec.metadata.labels onto the
    rendered pod template. Disagg adds the same to prefill.workerSpec.
    """
    out = dict(llmis_spec)
    if "workerSpec" in out:
        ws = dict(out["workerSpec"])
        ws.setdefault("metadata", {}).setdefault("labels", {})[key] = value
        out["workerSpec"] = ws
    if "prefill" in out and out["prefill"] and "workerSpec" in out["prefill"]:
        ws = dict(out["prefill"]["workerSpec"])
        ws.setdefault("metadata", {}).setdefault("labels", {})[key] = value
        out["prefill"] = {**out["prefill"], "workerSpec": ws}
    return out


def _gang_size(llmis_spec: dict) -> int:
    """Total pods in this LLM-IS's gang.

    Decode contributes (LWS size or 1) × workerSpec.replicas (instances).
    Prefill contributes the same if disagg.
    """
    total = _role_pods(llmis_spec)
    if "prefill" in llmis_spec and llmis_spec["prefill"]:
        total += _role_pods(llmis_spec["prefill"])
    return max(1, total)


def _role_pods(role_or_spec: dict) -> int:
    ws = role_or_spec.get("workerSpec") or {}
    instances = ws.get("replicas", 1) or 1
    lws = ws.get("leaderWorkerSet")
    nodes_per_inst = (lws or {}).get("size", 1) if lws else 1
    return nodes_per_inst * instances


def _kai_queue_name(namespace: str) -> str:
    """KAI Queues are per-Project; we map each Modelplane namespace to its
    own KAI Project + Queue, named after the namespace. The IC onboarding
    controller creates these when scheduler.type resolves to KAI."""
    return f"modelplane-{namespace}"


def _kueue_queue_name(namespace: str) -> str:
    """Per-namespace LocalQueue, again created by the IC onboarding
    controller. The LocalQueue references a ClusterQueue with the
    pool-class flavors the matcher saw."""
    return f"modelplane-{namespace}"
