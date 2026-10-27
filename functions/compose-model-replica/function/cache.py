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

"""Read a replica's ModelCache: how to mount it on the replica's cluster, or why it can't yet.

compose-model-cache publishes, for each cluster it has staged the model on, the
volumes, mounts and environment that read it there. A replica copies them into
its engine pods instead of working out where the model is.
"""

import dataclasses
from collections.abc import Iterator, Mapping

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1

# The name the replica requires its ModelCache under.
_REQUIREMENT = "cache"

# The volume a HuggingFace cache's mount names, and the variable it points at
# the mount. Must match compose-model-cache's _mount. Used only to read what an
# already running replica's engines mount, so they keep it while their cache
# publishes no mount.
_VOLUME = "model-cache"
_ENV = "HF_HUB_CACHE"


@dataclasses.dataclass
class Resolution:
    """How the replica reads its cache, or why it can't yet.

    mount is what the replica's engines mount, or None if they mount nothing
    yet. waiting is empty once the ModelCache publishes a mount for the
    replica's cluster, and otherwise says why it hasn't.
    """

    mount: mcv1alpha1.Mount | None
    waiting: str


def require(rsp: fnv1.RunFunctionResponse, replica: v1alpha1.ModelReplica) -> None:
    """Require the replica's ModelCache, which is in the replica's own namespace, if it reads one."""
    ref = replica.spec.modelCacheRef
    if not ref:
        return
    response.require_resources(
        rsp,
        name=_REQUIREMENT,
        api_version="modelplane.ai/v1alpha1",
        kind="ModelCache",
        match_name=ref.name,
        namespace=_namespace(replica),
    )


def resolve(req: fnv1.RunFunctionRequest, replica: v1alpha1.ModelReplica) -> Resolution | None:
    """How the replica reads its ModelCache on its cluster, or None if it reads no cache."""
    ref = replica.spec.modelCacheRef
    if not ref:
        return None
    # A requirement Crossplane hasn't resolved yet is absent from the request,
    # where one that matched nothing is present and empty.
    if _REQUIREMENT not in req.required_resources:
        return Resolution(mount=None, waiting=f"Waiting for ModelCache {ref.name}")
    found = request.get_required_resource(req, _REQUIREMENT)
    if found is None:
        return Resolution(mount=None, waiting=f"ModelCache {ref.name} not found in namespace {_namespace(replica)}")
    mc = mcv1alpha1.ModelCache.model_validate(found)
    cluster = replica.spec.clusterName
    entry = next((c for c in (mc.status.clusters if mc.status else None) or [] if c.name == cluster), None)
    if entry is None:
        return Resolution(mount=None, waiting=f"ModelCache {ref.name} isn't staged on cluster {cluster}")
    if entry.mount is not None:
        return Resolution(mount=entry.mount, waiting="")
    if entry.phase == "Ready":
        # Status written before the mount existed, until the cache's next
        # reconcile publishes one.
        waiting = f"Waiting for ModelCache {ref.name} to publish how to mount it on cluster {cluster}"
    else:
        waiting = f"ModelCache {ref.name} is {entry.phase or 'Pending'} on cluster {cluster}"
    if entry.message:
        waiting += f": {entry.message}"
    return Resolution(mount=None, waiting=waiting)


def kept(replica: v1alpha1.ModelReplica, observed: Mapping[str, fnv1.Resource], keys: list[str]) -> Resolution:
    """What an already running replica keeps mounting while its cache publishes no mount.

    The engines keep the cache volume, mount and env their workload has now,
    or no cache if it has none, so they don't roll onto a cache that isn't
    ready. waiting says so, the same way every reconcile.
    """
    ref = replica.spec.modelCacheRef
    assert ref is not None
    for key in keys:
        if key not in observed:
            continue
        obj = resource.struct_to_dict(observed[key].resource)
        mount = mounted(obj.get("spec", {}).get("forProvider", {}).get("manifest", {}))
        if mount is not None:
            return Resolution(
                mount=mount,
                waiting=f"ModelCache {ref.name} publishes no mount for cluster {replica.spec.clusterName}. "
                "The replica's engines keep the cache they mount now.",
            )
    return Resolution(
        mount=None,
        waiting=f"ModelCache {ref.name} publishes no mount for cluster {replica.spec.clusterName}. "
        "The replica's engines keep running without a cache.",
    )


def mounted(manifest: dict) -> mcv1alpha1.Mount | None:
    """The cache volume, mount and env a workload's pods have, or None if they mount no cache.

    A Deployment, a LeaderWorkerSet's leader and worker templates, and a
    PodCliqueSet's cliques hold their pod specs at different paths, and the
    replica mounts the same cache in each, so the first pod spec with the
    cache volume says what they all mount.
    """
    for pod in _pod_specs(manifest):
        volume = next((v for v in pod.get("volumes") or [] if v.get("name") == _VOLUME), None)
        if volume is None:
            continue
        # The engine container mounts the cache. Its sidecars don't.
        engine = next(
            (
                c
                for c in pod.get("containers") or []
                if any(m.get("name") == _VOLUME for m in c.get("volumeMounts") or [])
            ),
            {},
        )
        mounts = [
            mcv1alpha1.VolumeMount.model_validate(m)
            for m in engine.get("volumeMounts") or []
            if m.get("name") == _VOLUME
        ]
        # The cache's variables lead the engine's env, so the first HF_HUB_CACHE
        # is the cache's. One the engine sets itself comes later, and keeping it
        # too would add another copy on every reconcile.
        env = next((e for e in engine.get("env") or [] if e.get("name") == _ENV), None)
        return mcv1alpha1.Mount(
            volumes=[mcv1alpha1.Volume.model_validate(volume)],
            volumeMounts=mounts or None,
            env=[mcv1alpha1.EnvItem.model_validate(env)] if env else None,
        )
    return None


def _pod_specs(value: object) -> Iterator[dict]:
    """Every pod spec in a workload manifest, wherever its kind keeps it."""
    if isinstance(value, dict):
        if "containers" in value:
            yield value
            return
        for v in value.values():
            yield from _pod_specs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _pod_specs(v)


def _namespace(replica: v1alpha1.ModelReplica) -> str:
    """The replica's namespace, always set on a namespaced resource read from the API server."""
    if replica.metadata is None or replica.metadata.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return replica.metadata.namespace
