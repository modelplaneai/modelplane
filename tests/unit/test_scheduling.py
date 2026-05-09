"""Unit tests for the federation matcher (functions/compose-model-deployment/scheduling.py).

The matcher is pure Python over plain dataclasses — these tests run
without Crossplane, without K8s, without protos. CEL evaluation is
stubbed via a monkeypatch fixture so we can drive predicate outcomes
deterministically.
"""

import pytest

import scheduling


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_cel(monkeypatch):
    """By default, all CEL predicates pass. Tests that need to assert on
    CEL filtering override this with a per-test stub.
    """

    def _all_true(expr, capabilities):
        return True

    monkeypatch.setattr(scheduling, "eval_cel", _all_true)


def _ic(name, labels=None, pools=None):
    """Build an InferenceCluster fixture."""
    return scheduling.InferenceCluster(
        name=name,
        labels=labels or {},
        pools=pools or [],
    )


def _cls(name, gpu_count=8, capabilities=None):
    return scheduling.InferenceClass(
        name=name,
        capabilities=capabilities or {"gpu.count": gpu_count, "gpu.vramGiB": 80},
        gpu_count=gpu_count,
    )


def _pool(name, gpu_count=8, max_nodes=4, capabilities=None):
    return scheduling.Pool(
        name=name,
        cls=_cls(f"{name}-class", gpu_count=gpu_count, capabilities=capabilities),
        max_nodes=max_nodes,
    )


def _md(replicas=1, cluster_selector=None, decode=None, prefill=None):
    return scheduling.ModelDeploymentSpec(
        name="kimi",
        namespace="ml",
        cluster_selector=cluster_selector or {},
        replicas=replicas,
        decode=decode or scheduling.RoleSpec(
            node_selector_cel="",
            workers=scheduling.Workers(
                topology=scheduling.Topology(strategy="Tensor", tensor=8),
            ),
        ),
        prefill=prefill,
    )


# ---------------------------------------------------------------------------
# Topology shape (the per-strategy → (nodes_per_inst, gpus_per_node) math)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "kwargs", "expected_shape"),
    [
        ("Tensor", {"tensor": 1}, (1, 1)),
        ("Tensor", {"tensor": 8}, (1, 8)),
        ("TensorPipeline", {"tensor": 8, "pipeline": 2}, (2, 8)),
        ("TensorPipeline", {"tensor": 4, "pipeline": 4}, (4, 4)),
        ("DataExpert", {"tensor": 2, "data": 8, "data_local": 4}, (2, 8)),
    ],
)
def test_topology_shape(strategy, kwargs, expected_shape):
    t = scheduling.Topology(strategy=strategy, **kwargs)
    assert t.shape() == expected_shape


def test_topology_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown topology strategy"):
        scheduling.Topology(strategy="WhatNow", tensor=1).shape()


def test_role_nodes_required_multiplies_workers_count():
    role = scheduling.RoleSpec(
        node_selector_cel="",
        workers=scheduling.Workers(
            topology=scheduling.Topology(strategy="TensorPipeline", tensor=8, pipeline=2),
            count=3,
        ),
    )
    assert scheduling.role_nodes_required(role) == 6  # 2 nodes/worker * 3 workers


# ---------------------------------------------------------------------------
# Cluster-level filtering (matchLabels)
# ---------------------------------------------------------------------------


def test_match_filters_clusters_by_labels():
    eligible = _ic("east", labels={"tier": "production"}, pools=[_pool("p", max_nodes=4)])
    excluded = _ic("dev",  labels={"tier": "dev"}, pools=[_pool("p", max_nodes=4)])
    md = _md(cluster_selector={"tier": "production"})

    result = scheduling.schedule(md, [eligible, excluded], existing=[])

    placed = [p.cluster for p in result.placements]
    assert placed == ["east"]
    excluded_traces = [t for t in result.trace if t.cluster == "dev"]
    assert any(t.reason == "clusterSelector" for t in excluded_traces)


def test_match_no_eligible_clusters_returns_empty_with_trace():
    md = _md(cluster_selector={"tier": "frontier"})
    ic = _ic("east", labels={"tier": "production"}, pools=[_pool("p")])
    result = scheduling.schedule(md, [ic], existing=[])
    assert result.placements == []
    assert any(t.reason == "clusterSelector" for t in result.trace)


# ---------------------------------------------------------------------------
# Pool-level filtering (CEL + per-node shape + capacity)
# ---------------------------------------------------------------------------


def test_match_filters_pool_by_cel(monkeypatch):
    """Pool's class capabilities don't satisfy the role's CEL predicate."""
    md = _md(decode=scheduling.RoleSpec(
        node_selector_cel='capabilities["gpu.vramGiB"] >= 141',
        workers=scheduling.Workers(
            topology=scheduling.Topology(strategy="Tensor", tensor=8),
        ),
    ))
    ic = _ic("east", pools=[
        _pool("h100", capabilities={"gpu.count": 8, "gpu.vramGiB": 80}),
        _pool("h200", capabilities={"gpu.count": 8, "gpu.vramGiB": 141}),
    ])

    # Stub CEL: only h200 capabilities satisfy.
    def _cel(expr, caps):
        return caps.get("gpu.vramGiB", 0) >= 141

    monkeypatch.setattr(scheduling, "eval_cel", _cel)

    result = scheduling.schedule(md, [ic], existing=[])
    assert [p.decode.pool for p in result.placements] == ["h200"]


def test_match_pool_too_small_for_per_node_shape():
    """Pool has gpu.count < gpus_per_node. Static feasibility fails."""
    md = _md(decode=scheduling.RoleSpec(
        node_selector_cel="",
        workers=scheduling.Workers(
            topology=scheduling.Topology(strategy="Tensor", tensor=8),  # 8 GPUs/node
        ),
    ))
    ic = _ic("east", pools=[_pool("l4", gpu_count=4)])  # only 4 GPUs/node
    result = scheduling.schedule(md, [ic], existing=[])
    assert result.placements == []
    assert any(t.reason == "shape" for t in result.trace)


def test_match_pool_at_capacity():
    """Pool has 0 free nodes after existing placements consume max_nodes."""
    md = _md(decode=scheduling.RoleSpec(
        node_selector_cel="",
        workers=scheduling.Workers(
            topology=scheduling.Topology(strategy="Tensor", tensor=8),
        ),
    ))
    ic = _ic("east", pools=[_pool("h200", max_nodes=2)])
    existing = [
        scheduling.ExistingPlacement(
            md_name="other", md_namespace="ml", replica_index=0,
            cluster="east", decode_pool="h200", decode_nodes=2,
            prefill_pool=None, prefill_nodes=0,
        )
    ]
    result = scheduling.schedule(md, [ic], existing=existing)
    assert result.placements == []
    assert any(t.reason == "capacity" for t in result.trace)


# ---------------------------------------------------------------------------
# Capacity reservation across the same match() call
# ---------------------------------------------------------------------------


def test_match_capacity_reserved_across_replicas():
    """Multi-replica MD: subsequent replicas must see capacity consumed
    by replicas earlier in the SAME match() call."""
    md = _md(replicas=3, decode=scheduling.RoleSpec(
        node_selector_cel="",
        workers=scheduling.Workers(
            topology=scheduling.Topology(strategy="Tensor", tensor=8),
        ),
    ))
    # 2 ICs, each with a pool of max_nodes=2 → total capacity 4 replicas.
    ics = [
        _ic("east", pools=[_pool("p", max_nodes=2)]),
        _ic("west", pools=[_pool("p", max_nodes=2)]),
    ]
    result = scheduling.schedule(md, ics, existing=[])
    assert len(result.placements) == 3
    # Each pool can host 2; total fits 3.
    cluster_counts = {}
    for p in result.placements:
        cluster_counts[p.cluster] = cluster_counts.get(p.cluster, 0) + 1
    assert sum(cluster_counts.values()) == 3
    # Spread bonus prefers ICs we haven't placed on yet.
    assert len(cluster_counts) == 2


def test_match_oversubscribed_returns_partial():
    """4 replicas, only 2 pool-slots in the fleet → 2 placed, 2 dropped."""
    md = _md(replicas=4)
    ic = _ic("east", pools=[_pool("p", max_nodes=2)])
    result = scheduling.schedule(md, [ic], existing=[])
    assert len(result.placements) == 2


def test_match_zero_replicas():
    md = _md(replicas=0)
    ic = _ic("east", pools=[_pool("p")])
    result = scheduling.schedule(md, [ic], existing=[])
    assert result.placements == []


# ---------------------------------------------------------------------------
# Sticky placement: existing replicas keep their target.
# ---------------------------------------------------------------------------


def test_match_sticky_existing_replicas():
    md = _md(replicas=2)
    ics = [_ic("east", pools=[_pool("p", max_nodes=4)])]
    existing = [
        scheduling.ExistingPlacement(
            md_name="kimi", md_namespace="ml", replica_index=0,
            cluster="east", decode_pool="p", decode_nodes=1,
            prefill_pool=None, prefill_nodes=0,
        )
    ]
    result = scheduling.schedule(md, ics, existing=existing)
    placed = sorted(result.placements, key=lambda p: p.replica_index)
    assert placed[0].replica_index == 0
    assert placed[0].cluster == "east"
    assert placed[1].replica_index == 1


def test_match_sticky_survives_a_new_better_cluster():
    """A newer, less-saturated IC appearing should not move existing
    placements (sticky)."""
    md = _md(replicas=1)
    new_ic = _ic("west", pools=[_pool("p", max_nodes=10)])
    old_ic = _ic("east", pools=[_pool("p", max_nodes=2)])
    existing = [
        scheduling.ExistingPlacement(
            md_name="kimi", md_namespace="ml", replica_index=0,
            cluster="east", decode_pool="p", decode_nodes=1,
            prefill_pool=None, prefill_nodes=0,
        )
    ]
    result = scheduling.schedule(md, [old_ic, new_ic], existing=existing)
    assert [p.cluster for p in result.placements] == ["east"]


def test_match_drops_replicas_above_count():
    """If MD scaled down, existing replicas with high indices are dropped."""
    md = _md(replicas=1)  # scale down from 3 → 1
    ic = _ic("east", pools=[_pool("p", max_nodes=4)])
    existing = [
        scheduling.ExistingPlacement(
            md_name="kimi", md_namespace="ml", replica_index=i,
            cluster="east", decode_pool="p", decode_nodes=1,
            prefill_pool=None, prefill_nodes=0,
        )
        for i in range(3)
    ]
    result = scheduling.schedule(md, [ic], existing=existing)
    indices = sorted(p.replica_index for p in result.placements)
    assert indices == [0]


# ---------------------------------------------------------------------------
# Disaggregation: decode + prefill must land in the SAME cluster.
# ---------------------------------------------------------------------------


def test_match_disagg_skips_cluster_missing_prefill_pool(monkeypatch):
    """If a cluster has no pool satisfying the prefill CEL, the matcher
    must not place the disagg replica there even if decode would fit —
    decode + prefill must co-locate (KV cache transfer)."""
    md = _md(
        replicas=1,
        decode=scheduling.RoleSpec(
            node_selector_cel='capabilities["gpu.vramGiB"] >= 141',
            workers=scheduling.Workers(
                topology=scheduling.Topology(strategy="Tensor", tensor=8),
            ),
        ),
        prefill=scheduling.RoleSpec(
            node_selector_cel='capabilities["gpu.product"] == "L40S"',
            workers=scheduling.Workers(
                topology=scheduling.Topology(strategy="Tensor", tensor=1),
                count=2,
            ),
        ),
    )

    east = _ic("east", pools=[
        _pool("h200", gpu_count=8, max_nodes=4,
              capabilities={"gpu.count": 8, "gpu.vramGiB": 141, "gpu.product": "H200"}),
        _pool("l40s", gpu_count=4, max_nodes=4,
              capabilities={"gpu.count": 4, "gpu.vramGiB": 48, "gpu.product": "L40S"}),
    ])
    west = _ic("west", pools=[
        _pool("h200", gpu_count=8, max_nodes=4,
              capabilities={"gpu.count": 8, "gpu.vramGiB": 141, "gpu.product": "H200"}),
    ])

    # CEL stub: decode wants vramGiB>=141; prefill wants product=="L40S".
    def _cel(expr, caps):
        if "vramGiB" in expr:
            return caps.get("gpu.vramGiB", 0) >= 141
        if "L40S" in expr:
            return caps.get("gpu.product") == "L40S"
        return True

    monkeypatch.setattr(scheduling, "eval_cel", _cel)

    result = scheduling.schedule(md, [west, east], existing=[])

    assert len(result.placements) == 1
    p = result.placements[0]
    assert p.cluster == "east"
    assert p.decode.pool == "h200"
    assert p.prefill is not None
    assert p.prefill.pool == "l40s"


def test_match_disagg_capacity_split():
    """Disagg replica needs decode_nodes + prefill_nodes (different pools or
    same pool with combined headroom)."""
    md = _md(
        replicas=1,
        decode=scheduling.RoleSpec(
            node_selector_cel="",
            workers=scheduling.Workers(
                topology=scheduling.Topology(strategy="TensorPipeline", tensor=8, pipeline=2),
                count=3,
            ),
        ),
        prefill=scheduling.RoleSpec(
            node_selector_cel="",
            workers=scheduling.Workers(
                topology=scheduling.Topology(strategy="Tensor", tensor=1),
                count=5,
            ),
        ),
    )
    # decode needs 6 nodes, prefill needs 5.
    ic = _ic("east", pools=[
        _pool("h200", gpu_count=8, max_nodes=8),
        _pool("l40s", gpu_count=4, max_nodes=8),
    ])
    result = scheduling.schedule(md, [ic], existing=[])
    assert len(result.placements) == 1
    p = result.placements[0]
    assert p.decode.nodes_used == 6
    assert p.prefill.nodes_used == 5


# ---------------------------------------------------------------------------
# matchTrace shape — what the user sees on Ready=False
# ---------------------------------------------------------------------------


def test_match_trace_records_per_cluster_failures():
    md = _md(cluster_selector={"tier": "frontier"})
    ics = [
        _ic("east", labels={"tier": "production"}, pools=[_pool("p")]),
        _ic("west", labels={"tier": "dev"}, pools=[_pool("p")]),
    ]
    result = scheduling.schedule(md, ics, existing=[])
    by_cluster = {t.cluster for t in result.trace}
    assert by_cluster == {"east", "west"}


def test_match_trace_carries_capacity_detail():
    md = _md()
    ic = _ic("east", pools=[_pool("p", max_nodes=0)])
    result = scheduling.schedule(md, [ic], existing=[])
    capacity_traces = [t for t in result.trace if t.reason == "capacity"]
    assert capacity_traces
    assert "/0 free" in capacity_traces[0].detail or "0/" in capacity_traces[0].detail
