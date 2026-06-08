"""Tests for the compose-model-cache function."""

import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import json_format
from models.ai.modelplane.modelcache import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


def _cache_xr() -> v1alpha1.ModelCache:
    return v1alpha1.ModelCache(
        metadata=metav1.ObjectMeta(name="qwen", namespace="ml-team"),
        spec=v1alpha1.Spec(
            source=v1alpha1.Source(
                huggingFace=v1alpha1.HuggingFace(repo="Qwen/Qwen3-0.6B", sizeGiB=20),
            ),
        ),
    )


def _cluster_dict(name: str, pc: str, *, source: str = "GKE") -> dict:
    """An InferenceCluster as Crossplane returns it in a required-resource set.

    No cache block, so the function falls back to the source's XRD default
    storage class (GKE -> modelplane-rwx, EKS -> modelplane-rwx-efs).
    """
    blocks = {
        "GKE": {"gke": {"project": "my-project", "region": "us-central1"}},
        "EKS": {"eks": {"region": "us-west-2"}},
    }
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": name, "labels": {"modelplane.ai/cluster": "true"}},
        "spec": {"cluster": {"source": source, **blocks[source]}},
        "status": {"providerConfigRef": {"name": pc}},
    }


def _req(xr: v1alpha1.ModelCache, clusters: list[dict], observed: dict | None = None) -> fnv1.RunFunctionRequest:
    """Build a request the way the repo's other function tests do.

    - XR goes in observed.composite via dict_to_struct(model_dump(mode="json")).
    - Resolved clusters go in the `clusters` required-resource set.
    - `observed` maps a desired-resource key -> the remote status dict the
      provider echoes back (under Object.status.atProvider.manifest.status).
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json")),
            ),
        ),
    )
    for c in clusters:
        req.required_resources["clusters"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(c)),
        )
    for key, manifest_status in (observed or {}).items():
        req.observed.resources[key].resource.update(
            {"status": {"atProvider": {"manifest": {"status": manifest_status}}}},
        )
    return req


class TestModelCache(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_composes_pvc_and_job_per_cluster(self) -> None:
        rsp = await self.runner.RunFunction(
            _req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")]),
            None,
        )
        keys = set(rsp.desired.resources.keys())
        self.assertIn("pvc-cluster-a", keys)
        self.assertIn("hydrate-cluster-a", keys)

        pvc = json_format.MessageToDict(rsp.desired.resources["pvc-cluster-a"].resource)
        manifest = pvc["spec"]["forProvider"]["manifest"]
        self.assertEqual(manifest["kind"], "PersistentVolumeClaim")
        self.assertEqual(manifest["metadata"]["name"], "modelcache-ml-team-qwen")
        self.assertEqual(manifest["spec"]["accessModes"], ["ReadWriteMany"])
        self.assertEqual(manifest["spec"]["storageClassName"], "modelplane-rwx")
        self.assertEqual(manifest["spec"]["resources"]["requests"]["storage"], "20Gi")

    async def test_eks_cluster_pvc_uses_efs_storage_class(self) -> None:
        # An EKS cluster with no cache block falls back to the EFS default,
        # not the GKE/Filestore one. (EFS ignores the requested size — it's
        # elastic — but the PVC API still requires it.)
        rsp = await self.runner.RunFunction(
            _req(_cache_xr(), [_cluster_dict("eks-a", "eks-a-pc", source="EKS")]),
            None,
        )
        pvc = json_format.MessageToDict(rsp.desired.resources["pvc-eks-a"].resource)
        sc = pvc["spec"]["forProvider"]["manifest"]["spec"]["storageClassName"]
        self.assertEqual(sc, "modelplane-rwx-efs")
