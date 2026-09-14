---
title: Multi-node serving on Dynamo
weight: 30
description: Serve a model too large for one GPU across two nodes, gang-scheduled by the Dynamo stack.
---
<!-- vale write-good.Passive = NO -->
Qwen2.5-14B's FP16 weights are about 29 GB, larger than one NVIDIA L4's 23 GB, so
it serves across two nodes as a gang: a `Leader` and a `Worker`, one L4 each,
pipeline-parallel across the pair. On a
[Dynamo cluster]({{< ref "/platform/inference-cluster.md#serving-stack" >}}) Grove
and the KAI Scheduler gang-schedule the two pods together, Modelplane composes
them as a Grove `PodCliqueSet`, and NVIDIA ModelExpress serves their weights.

This is the [getting started tour]({{< ref "/getting-started" >}}) scaled to two
nodes: one larger model on a `spec.stack: Dynamo` cluster.
[Set up the platform]({{< ref "/getting-started/build-the-platform.md" >}}) first,
for the gateway and cloud credentials, then apply the manifests below.

## Register a Dynamo cluster

The `InferenceClass` describes a single-L4 node, sized up from the getting started
tour's for the larger model. The `InferenceCluster` runs two of them and sets
`spec.stack: Dynamo`, so Modelplane installs Grove, the KAI Scheduler, and
ModelExpress on the cluster.

{{< manifests "guides/serving-multi-node-on-dynamo/inference-class.yaml" >}}

{{< manifests "guides/serving-multi-node-on-dynamo/inference-cluster.yaml" >}}

Provisioning the pool and installing the stack takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/eks-us-east --timeout=20m
```

## Cache the weights

A gang reads its weights from a shared cache, so pods don't each pull a copy.
Create the namespace and the cache:

```bash
kubectl create namespace ml-team
```

{{< manifests "guides/serving-multi-node-on-dynamo/model-cache.yaml" >}}

## Deploy the gang

The `Leader` and `Worker` run the same `vllm serve`, differing only in node rank.
`$(MODELPLANE_LEADER_ADDRESS)` resolves to the leader on either stack, but
`$(MODELPLANE_RANK)` isn't injected on Dynamo yet, so the worker derives its rank
from Grove's `GROVE_PCLQ_POD_INDEX`.
[Multi-node deployments]({{< ref "/models/model-deployment.md#multi-node" >}})
covers this. Both load with `--load-format modelexpress`, so they read the cached
weights through the Dynamo stack's ModelExpress server.

{{< manifests "guides/serving-multi-node-on-dynamo/model-deployment.yaml" >}}

Wait until `READY` shows `True`. The first start hydrates the cache, so it's
slower than later ones:

```bash
kubectl get md -n ml-team --watch
```

On the workload cluster the gang is a Grove `PodCliqueSet`, the Dynamo stack's
multi-node workload in place of a LeaderWorkerSet:

```bash
kubectl get podcliquesets.grove.io -A   # workload cluster
```

## Expose and query

{{< manifests "guides/serving-multi-node-on-dynamo/model-service.yaml" >}}

Read the endpoint's address and send it a request. The `model` field is the
`--served-model-name` the deployment sets:

```bash
ADDRESS=$(kubectl get ms qwen2-5-14b -n ml-team -o jsonpath='{.status.address}')
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -s "$ADDRESS/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen2.5-14b\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}"'
```

The request routes through the gateway to the leader, which serves the gang's one
endpoint.

## Scale out with peer-to-peer loading

Add a second replica and ModelExpress shows what it's for. The first replica
seeds its weights from the cache and publishes itself as a source; the second
loads them straight from the first, peer-to-peer, rather than reading the cache
again.

Each replica is a gang of two nodes, so a second replica needs two more nodes.
Grow the pool to four, then scale the deployment:

```bash
kubectl patch ic/eks-us-east --type=json -p '[
  {"op":"replace","path":"/spec/nodePools/0/nodeCount","value":4},
  {"op":"replace","path":"/spec/nodePools/0/minNodeCount","value":4},
  {"op":"replace","path":"/spec/nodePools/0/maxNodeCount","value":4}]'
kubectl patch md/qwen2-5-14b -n ml-team --type=merge -p '{"spec":{"replicas":2}}'
```

Once the second gang starts, watch its leader load from the first over
ModelExpress:

```bash
kubectl logs -n default -l modelplane.ai/clique-role=leader -c engine --tail=-1 \
  | grep "source worker"   # workload cluster
# [Worker 0] Trying source worker 63d91022 (266 tensors)
```
<!-- vale write-good.Passive = YES -->
