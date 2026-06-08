"""Backend dispatch for compose-model-replica.

A backend turns a ModelReplica + its InferenceCluster into the cluster-level
serving resources. Backends return provider-kubernetes Objects and/or
provider-helm Releases; the dispatcher (fn.py) applies them to the response.
"""

from typing import Protocol

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# A composed resource is either a provider-kubernetes Object or a
# provider-helm Release. fn.py writes these into the response by key.
ComposedResource = k8sobjv1alpha1.Object | helmv1beta1.Release

# Backend identifiers.
NATIVE = "native"
LLMD = "llmd"
DYNAMO = "dynamo"


def engine_container(replica: v1alpha1.ModelReplica):
    """Return the container named 'engine'. The XRD's CEL validation
    guarantees exactly one exists, so this always succeeds.

    v0.1 constrains the template to a single container (the engine) via the
    XRD (containers maxItems: 1), so there is nothing to drop. Sidecar /
    multi-container support is tracked in #108 — it needs design for the LWS
    gang (which containers run on the leader vs the workers).
    """
    return next(c for c in replica.spec.workers.template.spec.containers if c.name == "engine")


def nodes_per_worker(replica: v1alpha1.ModelReplica) -> int:
    """Nodes spanned by one worker.

    v0.1 topology implements only tensor + pipeline, so this is `pipeline`.
    When data/dataLocal land, this becomes pipeline * (data / dataLocal).
    """
    return int(replica.spec.workers.topology.pipeline or 1)


def needs_cross_pod_coordination(replica: v1alpha1.ModelReplica) -> bool:
    """True when the replica is more than one self-contained pod.

    v0.1: true iff nodes_per_worker > 1. Extension points (no-ops until the
    fields exist): a `prefill` block (disaggregated P/D) or multi-node data
    parallelism (data > dataLocal) also make this true.
    """
    return nodes_per_worker(replica) > 1


def select_backend(replica: v1alpha1.ModelReplica) -> str:
    """Pick the lightest serving path. No user-facing backend field.

    Dynamo is dormant in v0.1: no Dynamo-only capability is wired, so a
    multi-pod replica always selects llm-d.
    """
    if not needs_cross_pod_coordination(replica):
        return NATIVE
    return LLMD


class Backend(Protocol):
    """Builds the cluster-level serving resources for one ModelReplica."""

    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
    ) -> dict[str, ComposedResource]:
        """Return a mapping of response resource-key -> composed resource.

        The caller (fn.py) must pass a cluster whose
        ``status.providerConfigRef.name`` is populated; backends read it to
        target the remote cluster and do not re-default it. Composed resources
        are named after ``replica.metadata.name`` (unique per placement) so
        co-located replicas don't collide.
        """
        ...
