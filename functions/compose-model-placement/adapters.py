"""Translate observed XR / extra-resource structs into rendering types.

Boundary between Crossplane's request shape and the pure-Python types the
renderer consumes. Sketch — until #64's protos are generated, the bodies
raise NotImplementedError.
"""

from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import rendering

# When #64 lands these become real:
#   from .lib import defaults
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1


def load_mr(req: fnv1.RunFunctionRequest) -> rendering.ModelReplicaView:
    """Project the observed ModelReplica XR into the renderer's view.

    Reads:  req.observed.composite.resource
    Returns: rendering.ModelReplicaView — placement decision + resolved
             decode/prefill role specs + engine + source.
    """
    raise NotImplementedError("wire to mrv1alpha1.ModelReplica when #64 lands")


def load_cluster(cluster_dict: dict) -> rendering.ClusterView:
    """Project an InferenceCluster dict into the renderer's view.

    Carries kubeconfig-secret ref, scheduler choice (proposed extension to
    #64: spec.scheduler.type), and pool→class mapping derived from
    spec.nodePools[].class.
    """
    raise NotImplementedError("walk cluster dict when #64 lands")


def load_classes(class_dicts: dict[str, dict]) -> dict[str, rendering.ClassView]:
    """Project InferenceClass dicts into ClassViews keyed by name."""
    raise NotImplementedError("walk class dicts when #64 lands")
