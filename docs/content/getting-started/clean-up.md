---
title: Clean up
weight: 60
description: Tear down everything you created during the tour.
---
Delete the model resources, the gateway, the clusters, and finally the control
plane.

## Delete model resources

Delete model resources before clusters. Deleting a cluster first leaves the
deployments reconciling against infrastructure that no longer exists.

```bash
kubectl delete md --all -n ml-team
kubectl delete ms --all -n ml-team
```

Wait for all model replicas to finish:

```bash
kubectl get modelreplica -n ml-team --watch
```

## Delete the gateway

Delete the gateway before its cluster. The `InferenceGateway` runs a load balancer
on the cluster it names; deleting it while that cluster is still up lets the load
balancer be removed, rather than leaking it when the cluster goes.

```bash
kubectl delete ig --all
```

## Delete the clusters

Delete all clusters with foreground cascading deletion. The serving stack on each
workload cluster must uninstall while that cluster's API server is still
reachable. Foreground deletion holds each cluster object until its stack
finishes. Background deletion can orphan cloud resources.

```bash
kubectl delete ic --all --cascade=foreground
```

Wait until all clusters are deleted:

```bash
kubectl get ic --watch
```

## Delete the control plane

Delete the kind cluster:

```bash
kind delete cluster --name modelplane
```
