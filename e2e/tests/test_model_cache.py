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

"""A replica waits for its ModelCache, then mounts it the way the cache says to.

The workload cluster has no ReadWriteMany storage, so the test brings a
StorageClass with no provisioner, and binds the cache's claim to a hostPath
volume it creates later. Until then the claim is Pending, and the replica
must wait. The cache stages a tiny public repository, filtered to its config
and tokenizer files.

The local pool has one fake GPU, which mock-demo holds, so the test scales
mock-demo to zero while it runs and back afterwards. Run it on its own, after
the rest of the suite: E2E_MODEL_CACHE=1 pytest e2e/tests/test_model_cache.py.
"""

import collections.abc
import os
import typing
import uuid

import pytest
from kubernetes import client

from e2e import kube, wait

pytestmark = pytest.mark.skipif(
    os.environ.get("E2E_MODEL_CACHE") != "1", reason="scales mock-demo to zero; set E2E_MODEL_CACHE=1 to run it"
)

NAMESPACE = "ml-team"
CLUSTER = "local"
CACHE = "tiny-gpt2"
DEPLOYMENT = "cached-mock"
REPO = "hf-internal-testing/tiny-random-gpt2"
STORAGE_CLASS = "e2e-model-cache"
VOLUME = "e2e-model-cache"
# Deleting the volume leaves its hostPath data on the node, and a cache that
# finds a previous run's completion marker skips its download. A path of its
# own per run makes every run download, and check, the files it filters.
HOST_PATH = f"/tmp/e2e-model-cache-{uuid.uuid4().hex[:8]}"
# Where the serving pods run on the workload cluster: the namespace mirroring
# ml-team.
REMOTE_NAMESPACE = "mp-ml-team-51733"
GROUP = "modelplane.ai"
# python:3.12-alpine pinned by digest, as the mock-demo engine.
IMAGE = "python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"

# The mock engine reports healthy only once the cache's snapshot of the repo
# is readable through HF_HUB_CACHE, so the replica can't go Ready on an empty
# or missing mount.
ENGINE = """
import glob, http.server, json, os
repo = "models--hf-internal-testing--tiny-random-gpt2"
snap = os.path.join(os.environ["HF_HUB_CACHE"], repo, "snapshots", "*", "config.json")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/health" and glob.glob(snap)
        b = json.dumps({"status": "ok" if ok else "no model"}).encode()
        self.send_response(200 if ok else 503)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
http.server.ThreadingHTTPServer(("", 8000), H).serve_forever()
"""


def _model_cache() -> dict[str, typing.Any]:
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelCache",
        "metadata": {"name": CACHE, "namespace": NAMESPACE},
        "spec": {
            "source": "HuggingFace",
            "huggingFace": {"repo": REPO, "include": ["*.json", "*.txt"], "exclude": ["*.msgpack"], "sizeGiB": 1},
        },
    }


def _model_deployment() -> dict[str, typing.Any]:
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "modelCacheRef": {"name": CACHE},
                    "engines": [
                        {
                            "name": "mock",
                            "members": [
                                {
                                    "role": "Standalone",
                                    "nodeSelector": {
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "count": 1,
                                                "selectors": [
                                                    {
                                                        "cel": 'device.capacity["gpu.example.com"].memory.'
                                                        'compareTo(quantity("20Gi")) >= 0'
                                                    }
                                                ],
                                            }
                                        ]
                                    },
                                    "template": {
                                        "spec": {
                                            "containers": [
                                                {
                                                    "name": "engine",
                                                    "image": IMAGE,
                                                    "command": ["python", "-u", "-c", ENGINE],
                                                }
                                            ]
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                }
            },
        },
    }


def _scale_mock_demo(control_plane: kube.Cluster, replicas: int) -> None:
    control_plane.custom.patch_namespaced_custom_object(
        GROUP,
        "v1alpha1",
        NAMESPACE,
        "modeldeployments",
        "mock-demo",
        {"spec": {"replicas": replicas}},
        _request_timeout=kube.TIMEOUT_SECONDS,
    )


def _set_cache_storage(control_plane: kube.Cluster, storage_class: str | None) -> None:
    control_plane.custom.patch_cluster_custom_object(
        GROUP,
        "v1alpha1",
        "inferenceclusters",
        CLUSTER,
        {"spec": {"cluster": {"existing": {"cache": {"storageClassName": storage_class}}}}},
        _request_timeout=kube.TIMEOUT_SECONDS,
    )


def _delete(fn: collections.abc.Callable[[], object]) -> None:
    try:
        fn()
    except client.ApiException as e:
        if e.status != 404:
            raise


@pytest.fixture(scope="module")
def cached(control_plane: kube.Cluster, workload: kube.Cluster) -> collections.abc.Iterator[None]:
    """A ModelCache whose claim can't bind yet, and a ModelDeployment that reads it."""
    storage = client.StorageV1Api(workload.core.api_client)
    storage.create_storage_class(
        client.V1StorageClass(
            metadata=client.V1ObjectMeta(name=STORAGE_CLASS),
            provisioner="kubernetes.io/no-provisioner",
            volume_binding_mode="Immediate",
        ),
        _request_timeout=kube.TIMEOUT_SECONDS,
    )
    _scale_mock_demo(control_plane, 0)
    _set_cache_storage(control_plane, STORAGE_CLASS)
    for plural, obj in (("modelcaches", _model_cache()), ("modeldeployments", _model_deployment())):
        control_plane.custom.create_namespaced_custom_object(
            GROUP, "v1alpha1", NAMESPACE, plural, obj, _request_timeout=kube.TIMEOUT_SECONDS
        )
    try:
        yield
    finally:
        for plural, name in (("modeldeployments", DEPLOYMENT), ("modelcaches", CACHE)):
            _delete(
                lambda plural=plural, name=name: control_plane.custom.delete_namespaced_custom_object(
                    GROUP, "v1alpha1", NAMESPACE, plural, name, _request_timeout=kube.TIMEOUT_SECONDS
                )
            )
        _delete(lambda: workload.core.delete_persistent_volume(VOLUME, _request_timeout=kube.TIMEOUT_SECONDS))
        _delete(lambda: storage.delete_storage_class(STORAGE_CLASS, _request_timeout=kube.TIMEOUT_SECONDS))
        _set_cache_storage(control_plane, None)
        _scale_mock_demo(control_plane, 1)


def _replica(control_plane: kube.Cluster) -> dict[str, typing.Any]:
    replicas = control_plane.custom.list_namespaced_custom_object(
        GROUP,
        "v1alpha1",
        NAMESPACE,
        "modelreplicas",
        label_selector=f"modelplane.ai/deployment={DEPLOYMENT}",
        _request_timeout=kube.TIMEOUT_SECONDS,
    )["items"]
    assert replicas, f"ModelDeployment {DEPLOYMENT} has no replica yet"
    return replicas[0]


def _condition(obj: dict[str, typing.Any], typ: str) -> dict[str, typing.Any]:
    return next((c for c in obj.get("status", {}).get("conditions", []) if c["type"] == typ), {})


def _cache_entry(control_plane: kube.Cluster) -> dict[str, typing.Any]:
    mc = control_plane.modelplane("modelcaches", CACHE, NAMESPACE)
    assert mc is not None, f"ModelCache {CACHE} doesn't exist"
    entry = next((c for c in mc.get("status", {}).get("clusters", []) if c["name"] == CLUSTER), None)
    assert entry is not None, f"ModelCache {CACHE} reports nothing for cluster {CLUSTER}"
    return entry


def _engine_pods(workload: kube.Cluster) -> list[client.V1Pod]:
    # The endpoint picker's pods carry the deployment label too.
    return workload.core.list_namespaced_pod(
        REMOTE_NAMESPACE,
        label_selector=f"modelplane.ai/deployment={DEPLOYMENT},modelplane.ai/engine=mock",
        _request_timeout=kube.TIMEOUT_SECONDS,
    ).items


@pytest.mark.usefixtures("cached")
def test_replica_waits_for_cache(control_plane: kube.Cluster, workload: kube.Cluster) -> None:
    """While the cache's claim can't bind, the cache says so and the replica starts no engine."""

    def waiting() -> str:
        entry = _cache_entry(control_plane)
        assert entry.get("phase") == "Pending", f"cache is {entry.get('phase')}"
        assert "to bind" in entry.get("message", ""), f"cache message is {entry.get('message')!r}"
        accepted = _condition(_replica(control_plane), "ModelAccepted")
        assert accepted.get("reason") == "WaitingForCache", f"replica ModelAccepted is {accepted}"
        return accepted["message"]

    message = wait.until(waiting, timeout=10 * 60, what="the replica to wait for its cache", retry=kube.RETRY)
    assert f"ModelCache {CACHE} is Pending on cluster {CLUSTER}" in message
    assert not _engine_pods(workload), "the replica started an engine before its cache was ready"


@pytest.mark.usefixtures("cached")
def test_replica_mounts_cache(control_plane: kube.Cluster, workload: kube.Cluster) -> None:
    """Once the claim binds and the cache hydrates, the replica mounts what the cache publishes."""
    workload.core.create_persistent_volume(
        client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(name=VOLUME),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": "1Gi"},
                access_modes=["ReadWriteMany"],
                storage_class_name=STORAGE_CLASS,
                persistent_volume_reclaim_policy="Delete",
                host_path=client.V1HostPathVolumeSource(path=HOST_PATH, type="DirectoryOrCreate"),
            ),
        ),
        _request_timeout=kube.TIMEOUT_SECONDS,
    )

    def mounted() -> dict[str, typing.Any]:
        entry = _cache_entry(control_plane)
        assert entry.get("phase") == "Ready", f"cache is {entry.get('phase')}: {entry.get('message')}"
        assert "mount" in entry, "a Ready cache publishes no mount"
        return entry["mount"]

    mount = wait.until(mounted, timeout=10 * 60, what="the cache to hydrate", retry=kube.RETRY)
    claim = mount["volumes"][0]["persistentVolumeClaim"]["claimName"]

    def serving() -> client.V1Pod:
        ready = _condition(_replica(control_plane), "ModelReady")
        assert ready.get("status") == "True", f"replica ModelReady is {ready}"
        pods = _engine_pods(workload)
        assert pods, "no engine pod"
        return pods[0]

    pod = wait.until(serving, timeout=10 * 60, what="the cached replica to be Ready", retry=kube.RETRY)
    claims = [v.persistent_volume_claim.claim_name for v in pod.spec.volumes if v.persistent_volume_claim]
    assert claims == [claim], f"engine pod mounts {claims}, the cache publishes {claim}"
    engine = next(c for c in pod.spec.containers if c.name == "engine")
    assert {"name": "HF_HUB_CACHE", "value": "/mnt/models"} in [{"name": e.name, "value": e.value} for e in engine.env]

    staged = workload.exec(
        pod.metadata.name,
        REMOTE_NAMESPACE,
        ["sh", "-c", "ls /mnt/models/models--hf-internal-testing--tiny-random-gpt2/snapshots/*/"],
    )
    assert staged.code == 0, staged.stderr
    files = staged.stdout.split()
    assert "config.json" in files, files
    assert not [f for f in files if f.endswith((".safetensors", ".bin", ".msgpack", ".h5"))], (
        f"the cache staged files its filters exclude: {files}"
    )
