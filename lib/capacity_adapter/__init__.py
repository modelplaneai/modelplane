"""Capacity adapter — per-scheduler status pullers.

Reads the in-cluster scheduler's status CRDs and normalizes them into
the shape `InferenceCluster.status.capacity` exposes to the federation
matcher. Same output schema across schedulers; the matcher reads one
shape regardless of which one is installed.

NOT a Crossplane composition function. Composition functions are
event-driven over the XR graph; this is a continuous poll / watch loop
against a remote cluster's CRDs. In production it runs as a separate
controller-runtime binary, one watcher per InferenceCluster, writing
back IC.status.capacity through the API server.

This package is sketch-quality — shows the per-scheduler logic and the
common output type. Wiring into a real controller is a follow-up.

Per-scheduler modules:

  kai.py      — KAI Queue.status + ResourcePool.status → CapacitySnapshot
  kueue.py    — Kueue ClusterQueue.status.flavorsUsage[] → CapacitySnapshot
  common.py   — shared dataclasses + the IC.status.capacity write helper

The Kubernetes deployment shape is:

  Per IC:                                   ┌── reconcile loop ──────────────┐
    onboarding controller writes            │  CapacityAdapter (kai/kueue)   │
    IC.status.detected.scheduler            │  watches the chosen scheduler  │
                       │                    │  → writes IC.status.capacity   │
                       └────────────────────│  every ~5s                     │
                                            └────────────────────────────────┘
"""
