"""Adapters: protobuf / Crossplane structs ⇄ scheduling.py dataclasses.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS THE BOUNDARY between Crossplane's request shape and the
  pure-Python types the matcher (scheduling.py) consumes.

  Pure (no I/O), but talks to Crossplane SDK structs in argument types.
  Stays separate from scheduling.py so the matcher tests don't have to
  fabricate protobuf messages.
═══════════════════════════════════════════════════════════════════════════

Three load functions, one direction each:

  load_md(req)          → scheduling.ModelDeploymentSpec
  load_clusters(req)    → list[scheduling.InferenceCluster]
                          (joins observed clusters + observed classes)
  load_existing(req)    → list[scheduling.ExistingPlacement]

Sketch — until #64's protos are generated, the bodies raise
NotImplementedError. The function signatures + docstrings document what
each adapter must produce, which is what the matcher relies on.
"""

from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduling

# When #64 lands these become real:
#   from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1


def load_md(req: fnv1.RunFunctionRequest) -> scheduling.ModelDeploymentSpec:
    """Project the observed ModelDeployment XR into the matcher's view.

    Reads:  req.observed.composite.resource (the MD XR struct)
    Returns: ModelDeploymentSpec — only fields the matcher uses.

    Adapter contract:
      - md.cluster_selector ← spec.clusterSelector.matchLabels (defaults to {})
      - md.replicas         ← spec.replicas (defaults to 1)
      - md.decode           ← RoleSpec from top-level nodeSelector + topology
      - md.prefill          ← RoleSpec from spec.prefill (None if absent)

    Disagg detection: presence of spec.prefill.

    Raises:
      ValueError on malformed topology (caught by main.py → ConfigInvalid).
    """
    raise NotImplementedError("wire to mdv1alpha1.ModelDeployment when #64 lands")


def load_clusters(
    req: fnv1.RunFunctionRequest,
) -> list[scheduling.InferenceCluster]:
    """Resolve InferenceClusters with their nodePools' classes inlined.

    Reads:
      req.extra_resources["clusters"] — InferenceCluster list
      req.extra_resources["classes"]  — InferenceClass list (referenced by
                                        nodePools[].class)

    Returns: list[InferenceCluster] with each pool's `cls` already
             populated from the matching InferenceClass.

    Adapter contract:
      - Pools whose class isn't observed yet are dropped (matcher won't
        consider them). Crossplane re-runs us when the class appears.
      - InferenceClass.spec.capabilities passes through verbatim — the
        CEL evaluator reads keys like "gpu.vramGiB", "gpu.features".
      - Pool.gpu_count is a convenience alias for capabilities["gpu.count"]
        used by the static feasibility check.
    """
    raise NotImplementedError("walk extra resources when #64 lands")


def load_existing(
    req: fnv1.RunFunctionRequest,
) -> list[scheduling.ExistingPlacement]:
    """Project owned ModelReplicas into matcher form.

    Reads: req.extra_resources["existing-replicas"] — ModelReplica list
           filtered by label modelplane.ai/deployment=<this MD's name>.

    Returns: list[ExistingPlacement] for capacity accounting + sticky
             placement.

    Adapter contract:
      - replica_index from MR.spec.replicaIndex
      - decode_pool / prefill_pool from MR.spec.target.{decodePool,
        prefillPool}
      - decode_nodes / prefill_nodes recomputed from the MR's stored
        topology (for capacity accounting; we don't trust user mutation
        of these fields).

    MRs that are mid-deletion or eviction-flagged are dropped — they're
    treated as new for the matcher (re-placement on hard eviction).
    """
    raise NotImplementedError("walk extra resources when #64 lands")
