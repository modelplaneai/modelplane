---
title: Drain a Cluster
weight: 35
description: Take an InferenceCluster out of service by tainting it, so Modelplane moves its replicas onto other clusters.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceCluster]({{< ref "/reference/inferenceclusters" >}}) · [`ModelDeployment`]({{< ref "/reference/modeldeployments" >}})

Taking a cluster out of service, for maintenance or decommissioning, means
telling Modelplane to stop scheduling there and, when you can't wait for work to
finish, to move what's already running. You do this by tainting the
`InferenceCluster`. It's the fleet-level counterpart to `kubectl drain` on a
node. Modelplane reschedules the affected replicas onto other clusters whose
hardware fits, the same way it placed them to begin with.

Taints follow the Kubernetes model and come in two effects:

- **`NoSchedule`** stops new replicas landing on the cluster but leaves the ones
  already there running. Existing work finishes on its own while nothing new
  arrives.
- **`NoExecute`** also moves the replicas already there. Modelplane deletes each
  one and reschedules it onto another cluster that fits.

## Taint the cluster

Add a taint to `spec.taints`, each with a `key`, an optional `value`, and an
`effect`:

```yaml {nocopy=true}
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: gke-us-east
spec:
  taints:
  - key: modelplane.ai/maintenance
    effect: NoSchedule    # NoExecute to also move running replicas off
  # cluster source and node pools unchanged
```

Removing the taint lets the cluster take work again. Nothing reschedules back on
its own: a taint only governs where new replicas can land, so replicas that
moved away stay where they went.

## What happens to running replicas

Under `NoExecute`, Modelplane reschedules each replica on the cluster the way it
schedules a new one, onto another cluster whose hardware satisfies the
deployment's device selectors and that isn't repelling the replica. The move
deletes the replica here and recreates it there, so the model reloads on the new
cluster and any requests still in flight to the old replica are dropped. The
deployment's other replicas keep serving while one moves.

When no other cluster can take a replica, because every candidate is full or
tainted, the deployment runs below its `spec.replicas` until capacity frees up.
Its `ReplicasScheduled` condition reports the shortfall, so a drain that can't
finish is visible rather than silent.

Under `NoSchedule`, running replicas stay put and only new placement is blocked.

## Keep a deployment through a drain

An ML team pins a critical deployment to a cluster through a drain by giving
it a matching toleration under `spec.template.spec.tolerations`. A replica that
tolerates a cluster's `NoSchedule` taint can still be placed there; one that
tolerates a `NoExecute` taint stays put when that taint is applied.

```yaml {nocopy=true}
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
spec:
  replicas: 2
  template:
    spec:
      tolerations:
      - key: modelplane.ai/maintenance
        operator: Exists    # Exists ignores value; Equal matches key and value
      # engines unchanged
```

A toleration matches a taint by `key` and `effect`. `operator: Exists` matches
any value for the key, while the default `Equal` matches key and value together;
an empty `key` with `Exists` tolerates every taint on the cluster. An empty
`effect` matches both effects. A replica is placed on, or left on, a tainted
cluster only when it tolerates every taint the cluster carries.

## Confirm the drain

Modelplane doesn't publish a per-cluster replica count. Check a drain the way
you check a drained node, by listing the replicas still placed on the cluster:

```bash
kubectl get modelreplica -l modelplane.ai/cluster=gke-us-east
```

Once that returns nothing, or only replicas that tolerate the taint and are
meant to stay, the drain is done. The cluster still refuses deletion while an
`InferenceGateway` runs on it, a `ModelService` routes through such a gateway, or
a `ModelCache` stages onto it. A taint doesn't move a cache; narrow its
`clusterSelector` to take it off the cluster.
