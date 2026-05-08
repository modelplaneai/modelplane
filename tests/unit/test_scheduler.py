"""Unit tests for per-scheduler wrap dispatch
(functions/compose-model-placement/scheduler.py).

Pure-Python tests over dict-shaped LLM-IS specs. No K8s, no Crossplane.
"""

import pytest

import scheduler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _llmis(decode_pods=1, prefill_pods=0, decode_replicas=1, prefill_replicas=0):
    """Build a minimal LLM-IS spec for testing.

    decode_pods, prefill_pods are the LWS group size (1 for single-pod,
    >1 for multi-node). decode_replicas, prefill_replicas are
    workerSpec.replicas (instances of the role).
    """
    spec = {
        "metadata": {"labels": {}},
        "model": {"name": "ml/kimi"},
        "replicas": 1,
        "engine": {"name": "vLLM"},
        "workerSpec": {
            "replicas": decode_replicas,
            "leaderWorkerSet": {"size": decode_pods} if decode_pods > 1 else None,
            "containers": [{"name": "engine"}],
        },
    }
    if prefill_pods > 0 or prefill_replicas > 0:
        spec["prefill"] = {
            "engine": {"name": "vLLM"},
            "workerSpec": {
                "replicas": max(prefill_replicas, 1),
                "leaderWorkerSet": {"size": prefill_pods} if prefill_pods > 1 else None,
                "containers": [{"name": "engine"}],
            },
        }
    return spec


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheduler_type", ["managed-kai", "kai"])
def test_dispatch_kai(scheduler_type):
    out = scheduler.wrap(scheduler_type, _llmis(), "kimi", "ml", 0)
    # KAI emits a PodGroup.
    assert any(o["kind"] == "PodGroup" for o in out.extra_objects)


@pytest.mark.parametrize("scheduler_type", ["managed-kueue", "kueue"])
def test_dispatch_kueue(scheduler_type):
    out = scheduler.wrap(scheduler_type, _llmis(), "kimi", "ml", 0)
    # Kueue emits no companion CR; the webhook handles it.
    assert out.extra_objects == []
    # And stamps the queue label.
    assert "kueue.x-k8s.io/queue-name" in out.llmis_spec["metadata"]["labels"]


def test_dispatch_none_passthrough():
    spec = _llmis()
    out = scheduler.wrap("none", spec, "kimi", "ml", 0)
    assert out.llmis_spec == spec
    assert out.extra_objects == []


def test_dispatch_unknown_falls_back_to_kueue():
    """Defensive default: unknown scheduler types use the safest backend."""
    out = scheduler.wrap("totally-not-a-real-scheduler", _llmis(), "kimi", "ml", 0)
    assert "kueue.x-k8s.io/queue-name" in out.llmis_spec["metadata"]["labels"]


# ---------------------------------------------------------------------------
# KAI specifics
# ---------------------------------------------------------------------------


def test_kai_sets_scheduler_name_on_decode_pod_template():
    out = scheduler.wrap_kai(_llmis(), "kimi", "ml", 0)
    assert out.llmis_spec["workerSpec"]["schedulerName"] == "kai-scheduler"


def test_kai_sets_scheduler_name_on_prefill_when_disagg():
    out = scheduler.wrap_kai(_llmis(prefill_pods=1, prefill_replicas=2), "kimi", "ml", 0)
    assert out.llmis_spec["prefill"]["workerSpec"]["schedulerName"] == "kai-scheduler"


def test_kai_emits_podgroup_with_min_member_for_single_pod():
    out = scheduler.wrap_kai(_llmis(decode_pods=1), "kimi", "ml", 0)
    [pg] = out.extra_objects
    assert pg["kind"] == "PodGroup"
    assert pg["spec"]["minMember"] == 1


def test_kai_podgroup_min_member_counts_lws_gang():
    """LWS group of size 2 → 2 pods per instance × 1 instance = 2."""
    out = scheduler.wrap_kai(_llmis(decode_pods=2, decode_replicas=1), "kimi", "ml", 0)
    [pg] = out.extra_objects
    assert pg["spec"]["minMember"] == 2


def test_kai_podgroup_min_member_counts_instances():
    """LWS=1, replicas=5 (e.g. P/D's prefill role) → 5 pods."""
    out = scheduler.wrap_kai(_llmis(decode_pods=1, decode_replicas=5), "kimi", "ml", 0)
    [pg] = out.extra_objects
    assert pg["spec"]["minMember"] == 5


def test_kai_podgroup_min_member_disagg_total():
    """5 prefill (1 pod each × 5) + 6 decode (2 pods × 3) = 11 total."""
    out = scheduler.wrap_kai(
        _llmis(decode_pods=2, decode_replicas=3, prefill_pods=1, prefill_replicas=5),
        "kimi", "ml", 0,
    )
    [pg] = out.extra_objects
    assert pg["spec"]["minMember"] == 11


def test_kai_stamps_pod_group_label_on_pod_template():
    """Pods need to carry the gang label so KAI can match them to the PodGroup."""
    out = scheduler.wrap_kai(_llmis(), "kimi", "ml", 0)
    labels = (
        out.llmis_spec["workerSpec"]
        .get("metadata", {})
        .get("labels", {})
    )
    assert labels.get("pod-group.scheduling.run.ai/name") == "kimi-gang"


def test_kai_queue_named_per_namespace():
    out = scheduler.wrap_kai(_llmis(), "kimi", "ml-team", 0)
    [pg] = out.extra_objects
    assert pg["spec"]["queue"] == "modelplane-ml-team"


# ---------------------------------------------------------------------------
# Kueue specifics
# ---------------------------------------------------------------------------


def test_kueue_stamps_queue_label():
    out = scheduler.wrap_kueue(_llmis(), "kimi", "ml-team", 0)
    assert out.llmis_spec["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "modelplane-ml-team"


def test_kueue_sets_suspend_true():
    """Kueue ungates the workload at admission time by flipping suspend → false."""
    out = scheduler.wrap_kueue(_llmis(), "kimi", "ml", 0)
    assert out.llmis_spec["suspend"] is True


def test_kueue_does_not_set_scheduler_name():
    """Kueue uses kube-scheduler — no scheduler name override."""
    out = scheduler.wrap_kueue(_llmis(), "kimi", "ml", 0)
    assert "schedulerName" not in out.llmis_spec.get("workerSpec", {})


# ---------------------------------------------------------------------------
# Gang-size accounting (used by KAI's PodGroup.minMember)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decode_pods", "decode_replicas", "prefill_pods", "prefill_replicas", "expected"),
    [
        (1, 1, 0, 0, 1),    # single-node, single replica
        (2, 1, 0, 0, 2),    # multi-node single replica (LWS=2)
        (1, 5, 0, 0, 5),    # 5 instances of single-pod
        (2, 3, 1, 5, 11),   # disagg: 2*3 + 1*5
        (8, 1, 0, 0, 8),    # giant single replica
    ],
)
def test_gang_size_calculation(decode_pods, decode_replicas, prefill_pods, prefill_replicas, expected):
    spec = _llmis(decode_pods, prefill_pods, decode_replicas, prefill_replicas)
    assert scheduler._gang_size(spec) == expected
