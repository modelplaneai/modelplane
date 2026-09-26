---
title: Clean up
weight: 60
description: Tear down everything you created during the tour.
---
Delete the model resources, the gateway, the clusters, and finally the control
plane.

## Delete model resources

Delete model resources before clusters. A cluster refuses deletion while
anything still runs on it. Foreground cascading deletion holds each resource
until what it composed on the clusters is gone, so a cluster isn't released
while that's still being removed:

```bash
kubectl delete md --all -n ml-team --cascade=foreground
kubectl delete ms --all -n ml-team --cascade=foreground
```

## Delete the gateway

Delete the gateway before its cluster. The `InferenceGateway` runs a load balancer
on the cluster it names; deleting it while that cluster is still up lets the load
balancer be removed, rather than leaking it when the cluster goes. Foreground
deletion holds the gateway until its objects on the cluster are deleted:

```bash
kubectl delete ig --all --cascade=foreground
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
