"""Translate observed XR / extra-resource structs into scheduling types.

Boundary between Crossplane's request shape and the pure-Python types the
matcher consumes. Sketch — until #64's protos are generated, the bodies
raise NotImplementedError. The function signatures + docstrings document
what each adapter must produce.
"""

from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduling

# When #64 lands these become real:
#   from .lib import defaults
#   from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1


def load_md(req: fnv1.RunFunctionRequest) -> scheduling.ModelDeploymentSpec:
    """Project the observed ModelDeployment XR into the scheduler's view.

    Reads:  req.observed.composite.resource
    Returns: scheduling.ModelDeploymentSpec — only the fields the scheduler
             reads (selectors, replicas, decode/prefill RoleSpec).

    Disagg detection: presence of spec.prefill.

    When wired:
        d = resource.struct_to_dict(req.observed.composite.resource)
        md = defaults.model_deployment(mdv1alpha1.ModelDeployment.model_validate(d))
        return _project_md(md)
    """
    raise NotImplementedError("wire to mdv1alpha1.ModelDeployment when #64 lands")


def resolve_clusters(
    cluster_dicts: list[dict],
    class_dicts: list[dict],
) -> list[scheduling.InferenceCluster]:
    """Resolve InferenceClusters with each pool's class capabilities inlined.

    Pools whose class isn't observed yet are dropped — Crossplane re-runs
    when the class appears.
    """
    raise NotImplementedError("walk cluster + class dicts when #64 lands")


def resolve_existing(replica_dicts: list[dict]) -> list[scheduling.ExistingPlacement]:
    """Project owned ModelReplicas into matcher form for sticky placement.

    Replicas mid-deletion or evict-flagged are dropped; the matcher treats
    those indices as new and re-picks (re-placement on hard eviction).
    """
    raise NotImplementedError("walk replica dicts when #64 lands")
