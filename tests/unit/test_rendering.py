"""Unit tests for pure rendering
(functions/compose-model-placement/rendering.py).

Pure dict-builders — no Crossplane, no K8s.
"""

import pytest

import rendering


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _role(strategy="Tensor", tensor=8, pipeline=0, instances=1, gpus_per_node=8, pool="h200"):
    return rendering.RoleView(
        topology={
            "strategy": strategy,
            "tensor": tensor,
            "pipeline": pipeline,
            "data": 0,
            "dataLocal": 0,
            "instances": instances,
        },
        node_selector_cel="",
        pool=pool,
        nodes_used=pipeline or 1,
        gpus_per_node=gpus_per_node,
        instances=instances,
    )


def _cls(name="h200-class", capabilities=None):
    return rendering.ClassView(
        name=name,
        capabilities=capabilities or {
            "gpu.vendor": "nvidia",
            "gpu.product": "H200",
            "gpu.vramGiB": 141,
            "gpu.features": ["fp8", "bf16"],
        },
    )


def _mr(prefill=None, target_prefill_pool=None):
    return rendering.ModelReplicaView(
        parent_name="kimi",
        parent_namespace="ml",
        replica_index=0,
        target_cluster="east",
        target_decode_pool="h200",
        target_prefill_pool=target_prefill_pool,
        decode=_role(),
        prefill=prefill,
        engine={"name": "vLLM", "image": "vllm/vllm-openai:v0.8.5", "args": ["--max-model-len=131072"]},
        source={"type": "HuggingFace", "huggingFace": {"repo": "moonshotai/Kimi-K2-Instruct"}},
    )


# ---------------------------------------------------------------------------
# build_llmis_spec — single-node Tensor
# ---------------------------------------------------------------------------


def test_llmis_single_node_tensor_no_lws():
    mr = _mr()
    spec = rendering.build_llmis_spec(mr, {"h200": _cls()})
    assert spec["replicas"] == 1
    assert spec["workerSpec"]["leaderWorkerSet"] is None
    assert spec["workerSpec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert "prefill" not in spec


def test_llmis_multi_node_emits_lws():
    mr = _mr()
    mr.decode = _role(strategy="TensorPipeline", tensor=8, pipeline=2, gpus_per_node=8)
    spec = rendering.build_llmis_spec(mr, {"h200": _cls()})
    assert spec["workerSpec"]["leaderWorkerSet"] == {"size": 2}


def test_llmis_engine_args_pass_through():
    mr = _mr()
    spec = rendering.build_llmis_spec(mr, {"h200": _cls()})
    assert "--max-model-len=131072" in spec["engine"]["args"]


def test_llmis_model_name_is_namespace_slash_name():
    mr = _mr()
    spec = rendering.build_llmis_spec(mr, {"h200": _cls()})
    assert spec["model"]["name"] == "ml/kimi"


# ---------------------------------------------------------------------------
# build_llmis_spec — disaggregated
# ---------------------------------------------------------------------------


def test_llmis_disagg_has_prefill_block():
    prefill = _role(strategy="Tensor", tensor=1, instances=5, gpus_per_node=1, pool="l40s")
    mr = _mr(prefill=prefill, target_prefill_pool="l40s")
    spec = rendering.build_llmis_spec(mr, {"h200": _cls(), "l40s": _cls(name="l40s-class")})
    assert "prefill" in spec
    assert spec["prefill"]["workerSpec"]["replicas"] == 5
    assert spec["prefill"]["workerSpec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1


def test_llmis_disagg_each_role_has_dra_claim_ref():
    prefill = _role(strategy="Tensor", tensor=1, instances=5, gpus_per_node=1, pool="l40s")
    mr = _mr(prefill=prefill, target_prefill_pool="l40s")
    spec = rendering.build_llmis_spec(mr, {"h200": _cls(), "l40s": _cls()})
    assert spec["workerSpec"]["containers"][0]["resources"]["claims"] == [{"name": "gpus"}]
    assert spec["prefill"]["workerSpec"]["containers"][0]["resources"]["claims"] == [{"name": "gpus"}]


# ---------------------------------------------------------------------------
# build_resource_claim_spec
# ---------------------------------------------------------------------------


def test_resource_claim_count_matches_gpus_per_node():
    role = _role(gpus_per_node=8)
    claim = rendering.build_resource_claim_spec(role, _cls())
    assert claim["devices"]["requests"][0]["count"] == 8


def test_resource_claim_device_class_for_amd():
    cls = _cls(capabilities={"gpu.vendor": "amd", "gpu.product": "MI300X"})
    claim = rendering.build_resource_claim_spec(_role(), cls)
    assert claim["devices"]["requests"][0]["deviceClassName"] == "gpu.amd.com"


def test_resource_claim_device_class_for_nvidia_default():
    """No vendor → default to nvidia."""
    cls = _cls(capabilities={"gpu.product": "H100"})
    claim = rendering.build_resource_claim_spec(_role(), cls)
    assert claim["devices"]["requests"][0]["deviceClassName"] == "gpu.nvidia.com"


# ---------------------------------------------------------------------------
# cel_from_capabilities — DRA selector derivation
# ---------------------------------------------------------------------------


def test_cel_includes_vendor():
    cel = rendering.cel_from_capabilities({"gpu.vendor": "nvidia"})
    assert 'device.driver == "nvidia.com"' in cel


def test_cel_includes_vram_predicate():
    cel = rendering.cel_from_capabilities({"gpu.vendor": "nvidia", "gpu.vramGiB": 141})
    assert 'device.attributes["nvidia.com/memory.gib"].int >= 141' in cel


def test_cel_features_become_in_predicates():
    cel = rendering.cel_from_capabilities({
        "gpu.vendor": "nvidia",
        "gpu.features": ["fp8", "bf16"],
    })
    assert '"fp8" in device.attributes["nvidia.com/features"].listString' in cel
    assert '"bf16" in device.attributes["nvidia.com/features"].listString' in cel


def test_cel_empty_capabilities_yields_true():
    cel = rendering.cel_from_capabilities({})
    assert cel == "true"


def test_cel_predicates_are_anded():
    cel = rendering.cel_from_capabilities({
        "gpu.vendor": "nvidia",
        "gpu.product": "H200",
        "gpu.vramGiB": 141,
    })
    # Two ' && ' joining three clauses.
    assert cel.count(" && ") == 2
