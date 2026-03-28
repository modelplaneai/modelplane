"""Populate default values on Pydantic models.

Optional nested fields (status sub-objects, optional spec groups) require
guard chains at every access site. These functions return a deep copy with
all intermediate Optional fields populated with their zero values, so
downstream code can access fields directly.
"""

from ..model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from ..model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from ..model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from ..model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1


def inference_environment(
    ie: iev1alpha1.InferenceEnvironment,
) -> iev1alpha1.InferenceEnvironment:
    """Return a copy with status fields defaulted."""
    ie = ie.model_copy(deep=True)
    ie.status = ie.status or iev1alpha1.Status()
    ie.status.providerConfigRef = ie.status.providerConfigRef or iev1alpha1.ProviderConfigRef()
    ie.status.gateway = ie.status.gateway or iev1alpha1.Gateway()
    ie.status.capacity = ie.status.capacity or iev1alpha1.Capacity()
    ie.status.capacity.gpuPools = ie.status.capacity.gpuPools or []
    return ie


def cluster_model(
    model: cmv1alpha1.ClusterModel,
) -> cmv1alpha1.ClusterModel:
    """Return a copy with optional spec fields defaulted.

    Most spec defaults (cpu, memory, image) are already applied by Pydantic
    from the OpenAPI schema. This function only handles fields where the
    parent object is Optional (spec.vllm) or where we want a different zero
    value (extraArgs as [] instead of None).
    """
    model = model.model_copy(deep=True)
    model.spec.vllm = model.spec.vllm or cmv1alpha1.Vllm()
    model.spec.vllm.extraArgs = model.spec.vllm.extraArgs or []
    return model


def model_placement(
    p: mpv1alpha1.ModelPlacement,
) -> mpv1alpha1.ModelPlacement:
    """Return a copy with status fields defaulted."""
    p = p.model_copy(deep=True)
    p.metadata = p.metadata or mpv1alpha1.ModelPlacement().metadata
    p.status = p.status or mpv1alpha1.Status()
    p.status.resources = p.status.resources or mpv1alpha1.Resources()
    p.status.resources.gpu = p.status.resources.gpu or mpv1alpha1.Gpu()
    return p


def inference_gateway(
    gw: igwv1alpha1.InferenceGateway,
) -> igwv1alpha1.InferenceGateway:
    """Return a copy with status fields defaulted."""
    gw = gw.model_copy(deep=True)
    gw.status = gw.status or igwv1alpha1.Status()
    return gw
