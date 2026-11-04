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

"""Tests for compose-model-replica's reading of a replica's ModelCache."""

import dataclasses

import pytest
from function import cache
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1


@dataclasses.dataclass
class MountedCase:
    """A test case for cache.mounted."""

    name: str
    reason: str
    manifest: dict
    want: mcv1alpha1.Mount | None


def _pod_spec(*, claim_name: str | None) -> dict:
    """An engine pod's spec, mounting the named cache claim if any, beside the shared memory every engine mounts."""
    engine: dict = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
    }
    volumes: list[dict] = [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]
    if claim_name is not None:
        engine["env"] = [
            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
            {"name": "MX_MODEL_REVISION", "value": claim_name},
        ]
        engine["volumeMounts"].append({"name": "model-cache", "mountPath": "/mnt/models"})
        volumes.append({"name": "model-cache", "persistentVolumeClaim": {"claimName": claim_name}})
    return {"containers": [{"name": "sidecar", "image": "busybox:1.36"}, engine], "volumes": volumes}


def _mount(*, claim_name: str) -> mcv1alpha1.Mount:
    """The mount a HuggingFace ModelCache publishes, reading the named claim."""
    return mcv1alpha1.Mount(
        volumes=[
            mcv1alpha1.Volume(
                name="model-cache", persistentVolumeClaim=mcv1alpha1.PersistentVolumeClaim(claimName=claim_name)
            )
        ],
        volumeMounts=[mcv1alpha1.VolumeMount(name="model-cache", mountPath="/mnt/models")],
        env=[mcv1alpha1.EnvItem(name="HF_HUB_CACHE", value="/mnt/models")],
    )


MOUNTED_CASES = [
    MountedCase(
        name="Deployment",
        reason="A Deployment's pod template mounts the cache its replica composed it with.",
        manifest={
            "kind": "Deployment",
            "spec": {"template": {"spec": _pod_spec(claim_name="modelcache-ml-team-qwen-17db2")}},
        },
        want=_mount(claim_name="modelcache-ml-team-qwen-17db2"),
    ),
    MountedCase(
        name="LeaderWorkerSet",
        reason="A LeaderWorkerSet's leader and worker templates mount the cache its replica composed it with.",
        manifest={
            "kind": "LeaderWorkerSet",
            "spec": {
                "leaderWorkerTemplate": {
                    "leaderTemplate": {"spec": _pod_spec(claim_name="modelcache-ml-team-kimi-aa322")},
                    "workerTemplate": {"spec": _pod_spec(claim_name="modelcache-ml-team-kimi-aa322")},
                }
            },
        },
        want=_mount(claim_name="modelcache-ml-team-kimi-aa322"),
    ),
    MountedCase(
        name="PodCliqueSet",
        reason="A PodCliqueSet's cliques mount the cache its replica composed it with.",
        manifest={
            "kind": "PodCliqueSet",
            "spec": {
                "template": {
                    "cliques": [
                        {"name": "leader", "spec": {"podSpec": _pod_spec(claim_name="modelcache-ml-team-c-c5cc6")}},
                        {"name": "worker", "spec": {"podSpec": _pod_spec(claim_name="modelcache-ml-team-c-c5cc6")}},
                    ]
                }
            },
        },
        want=_mount(claim_name="modelcache-ml-team-c-c5cc6"),
    ),
    MountedCase(
        name="EngineSetsCacheEnv",
        reason="An engine's own HF_HUB_CACHE follows the cache's, and isn't part of the cache's mount.",
        manifest={
            "kind": "Deployment",
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "engine",
                                "image": "vllm/vllm-openai:latest",
                                "env": [
                                    {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                    {"name": "HF_HUB_CACHE", "value": "/mnt/models/hub"},
                                ],
                                "volumeMounts": [{"name": "model-cache", "mountPath": "/mnt/models"}],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "model-cache",
                                "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                            }
                        ],
                    }
                }
            },
        },
        want=_mount(claim_name="modelcache-ml-team-qwen-17db2"),
    ),
    MountedCase(
        name="NoCache",
        reason="A workload whose pods mount no cache has no cache mount.",
        manifest={"kind": "Deployment", "spec": {"template": {"spec": _pod_spec(claim_name=None)}}},
        want=None,
    ),
    MountedCase(
        name="NoPods",
        reason="A workload manifest with no pod template, as an observe-only scaling group has, has no cache mount.",
        manifest={"kind": "PodCliqueScalingGroup"},
        want=None,
    ),
]


@pytest.mark.parametrize("case", MOUNTED_CASES, ids=lambda case: case.name)
def test_mounted(case: MountedCase) -> None:
    """A replica reads the cache volume, mount and env its workload's pods already have."""
    assert cache.mounted(case.manifest) == case.want, case.reason
