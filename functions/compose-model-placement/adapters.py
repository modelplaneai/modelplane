"""Adapters: protobuf / Crossplane structs ⇄ rendering.py dataclasses.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS THE BOUNDARY between Crossplane's request shape and the
  pure-Python types the renderer (rendering.py) consumes.

  Sketch — until #64's protos are generated, the bodies raise
  NotImplementedError. The function signatures + docstrings document
  what each adapter must produce.
═══════════════════════════════════════════════════════════════════════════
"""

from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import rendering


def load_mr(req: fnv1.RunFunctionRequest) -> rendering.ModelReplicaView:
    """Project the observed ModelReplica XR into the renderer's view.

    Reads:  req.observed.composite.resource (the MR XR struct)
    Returns: rendering.ModelReplicaView — only what the renderer needs.

    Adapter contract:
      - Reads MR.spec.target.{cluster, decodePool, prefillPool}
      - Reads MR.spec.{decode, prefill, engine, source}
      - Reads MR.metadata's parent labels for parent_name / namespace
        (or MR.spec.parentRef.name once that field exists in #64).
    """
    raise NotImplementedError("wire to mrv1alpha1.ModelReplica when #64 lands")


def load_cluster(req: fnv1.RunFunctionRequest) -> rendering.ClusterView:
    """Project the observed InferenceCluster into the renderer's view.

    Reads: req.extra_resources["cluster"] — the IC matching MR.spec.target.cluster.
    Returns: rendering.ClusterView — kubeconfig secret ref + scheduler choice
             + pool→class mapping.

    Adapter contract:
      - kubeconfig_secret_ref ← spec.cluster.existing.secretRef (or
        composed equivalent for managed clusters).
      - scheduler_type ← spec.scheduler.type (proposed extension to #64;
        defaults to "managed-kueue" if absent).
      - pool_to_class ← {p.name: p.class for p in spec.nodePools}.
    """
    raise NotImplementedError("walk extra resources when #64 lands")


def load_classes(
    req: fnv1.RunFunctionRequest, names: list[str]
) -> dict[str, rendering.ClassView]:
    """Resolve the named InferenceClasses into ClassView dataclasses.

    Reads: req.extra_resources[f"class-{name}"] for each requested name.
    Returns: dict[name → ClassView].

    Adapter contract:
      - capabilities ← spec.capabilities verbatim (open key/value map).
      - Unobserved classes raise KeyError; the orchestrator catches and
        keeps waiting via require_resources.
    """
    raise NotImplementedError("walk extra resources when #64 lands")
