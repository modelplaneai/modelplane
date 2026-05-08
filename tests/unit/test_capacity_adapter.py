"""Unit tests for capacity adapters
(lib/capacity_adapter/{kai,kueue,common}.py).

The pure projection functions (`_project_pool`) are testable directly with
sample status dicts. The K8s-client orchestration (`snapshot()`) raises
NotImplementedError until wired.
"""

from datetime import datetime

import pytest

from lib.capacity_adapter import common, kai, kueue


# ---------------------------------------------------------------------------
# common.ResourceCount + write_status
# ---------------------------------------------------------------------------


def test_resource_count_available_subtracts_used():
    r = common.ResourceCount(name="nvidia.com/gpu", total=32, used=12)
    assert r.available == 20


def test_resource_count_available_clamped_to_zero():
    """Used can transiently exceed total during reservation churn — never
    return a negative."""
    r = common.ResourceCount(name="nvidia.com/gpu", total=8, used=10)
    assert r.available == 0


def test_write_status_shape_round_trip():
    snap = common.CapacitySnapshot(
        cluster="east",
        pools=[
            common.PoolCapacity(
                name="frontier",
                resources=[common.ResourceCount(name="nvidia.com/gpu", total=32, used=12)],
            )
        ],
        last_observed=datetime(2026, 5, 8, 12, 0, 0),
    )
    out = common.write_status(snap)
    assert out["status"]["capacity"]["lastObserved"] == "2026-05-08T12:00:00Z"
    [pool] = out["status"]["capacity"]["pools"]
    assert pool["name"] == "frontier"
    [res] = pool["resources"]
    assert res == {"name": "nvidia.com/gpu", "total": 32, "used": 12, "available": 20}


# ---------------------------------------------------------------------------
# KAI projection
# ---------------------------------------------------------------------------


def test_kai_project_pool_reads_quota_and_allocated():
    raw = {
        "metadata": {"labels": {"modelplane.ai/pool": "frontier"}},
        "status": {
            "resources": [
                {"name": "nvidia.com/gpu", "quota": 32, "allocated": 12},
                {"name": "cpu", "quota": 256, "allocated": 120},
            ]
        },
    }
    pool = kai._project_pool("frontier", raw)
    assert pool.name == "frontier"
    assert pool.resources[0].available == 20
    assert pool.resources[1].available == 136


def test_kai_pool_to_modelplane_uses_label():
    raw = {"metadata": {"labels": {"modelplane.ai/pool": "frontier"}}}
    assert kai._kai_pool_to_modelplane(raw) == "frontier"


def test_kai_pool_to_modelplane_returns_none_when_unlabeled():
    raw = {"metadata": {"labels": {"other": "x"}}}
    assert kai._kai_pool_to_modelplane(raw) is None


# ---------------------------------------------------------------------------
# Kueue projection
# ---------------------------------------------------------------------------


def test_kueue_project_pool_parses_total_and_usage_quantities():
    """ClusterQueue.flavorsUsage values come as Quantity strings."""
    flavor_usage = {
        "name": "h200",
        "resources": [
            {"name": "nvidia.com/gpu", "total": "32", "usage": "12"},
            {"name": "cpu", "total": "256", "usage": "120"},
        ],
    }
    pool = kueue._project_pool("frontier", flavor_usage)
    assert pool.resources[0].name == "nvidia.com/gpu"
    assert pool.resources[0].available == 20
    assert pool.resources[1].available == 136


def test_kueue_parse_quantity_handles_int_and_string():
    assert kueue._parse_quantity(42) == 42
    assert kueue._parse_quantity("42") == 42


def test_kueue_project_pool_empty_resources():
    pool = kueue._project_pool("frontier", {"name": "h200", "resources": []})
    assert pool.resources == []


# ---------------------------------------------------------------------------
# snapshot() — wiring NotImplementedError check (sanity)
# ---------------------------------------------------------------------------


def test_kai_snapshot_raises_until_wired():
    with pytest.raises(NotImplementedError):
        kai.snapshot("east", k8s_client=None)


def test_kueue_snapshot_raises_until_wired():
    with pytest.raises(NotImplementedError):
        kueue.snapshot("east", k8s_client=None)
