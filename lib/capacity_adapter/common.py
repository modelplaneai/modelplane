"""Shared types for capacity adapters.

All scheduler-specific adapters return the same shape; the IC's
status.capacity is the union of what every adapter has written. The
federation matcher reads `pools[].resources[].available` to score
candidate placements.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ResourceCount:
    """A single resource line in IC.status.capacity.pools[].resources[].

    Resource name follows K8s conventions:
      - nvidia.com/gpu, amd.com/gpu, google.com/tpu  (whole devices)
      - nvidia.com/mig-1g.10gb                        (MIG slices)
      - cpu, memory                                   (host shape)

    For the matcher the load-bearing field is `available`. `total` and
    `used` are observability for the operator.
    """

    name: str
    total: int
    used: int

    @property
    def available(self) -> int:
        return max(0, self.total - self.used)


@dataclass
class PoolCapacity:
    """Capacity for one InferenceCluster.spec.nodePools[] entry."""

    name: str
    resources: list[ResourceCount] = field(default_factory=list)


@dataclass
class CapacitySnapshot:
    """One adapter run's view of an entire InferenceCluster."""

    cluster: str
    pools: list[PoolCapacity] = field(default_factory=list)
    last_observed: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Status writer — the controller calls this each tick.
# ---------------------------------------------------------------------------


def write_status(snapshot: CapacitySnapshot) -> dict:
    """Build the InferenceCluster.status.capacity payload to PATCH.

    Real impl uses a typed Kubernetes client; sketch returns the dict.
    Idempotent — same snapshot in, same payload out.
    """
    last_observed_str = snapshot.last_observed.replace(tzinfo=None).isoformat() + "Z"
    return {
        "status": {
            "capacity": {
                "lastObserved": last_observed_str,
                "pools": [
                    {
                        "name": p.name,
                        "resources": [
                            {
                                "name": r.name,
                                "total": r.total,
                                "used": r.used,
                                "available": r.available,
                            }
                            for r in p.resources
                        ],
                    }
                    for p in snapshot.pools
                ],
            }
        }
    }
