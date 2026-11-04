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
a `DynamoGraphDeployment` with a frontend and a multi-node engine component, and NVIDIA ModelExpress serves their weights.

This is the [getting started tour]({{< ref "/getting-started" >}}) scaled to two
nodes: one larger model on a `spec.stack: Dynamo` cluster.
[Set up the platform]({{< ref "/getting-started/build-the-platform.md" >}}) first,
for the gateway and cloud credentials, then apply the manifests below.

## Register a Dynamo cluster

The `InferenceClass` describes a single-L4 node, sized up from the getting started
tour's for the larger model. The `InferenceCluster` runs two of them and sets
`spec.stack: Dynamo`, so Modelplane installs the Dynamo operator, Grove, the KAI Scheduler, and
ModelExpress on the cluster.

{{< manifests "guides/serving-multi-node-on-dynamo/inference-class.yaml" >}}

{{< manifests "guides/serving-multi-node-on-dynamo/inference-cluster.yaml" >}}

Provisioning the pool and installing the stack takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/eks-us-east-dynamo --timeout=20m
```

## Cache the weights

A gang reads its weights from a shared cache, so pods don't each pull a copy.
Create the namespace and the cache:

```bash
kubectl create namespace ml-team
```

{{< manifests "guides/serving-multi-node-on-dynamo/model-cache.yaml" >}}

## Deploy the gang

The `Leader` and `Worker` run `python3 -m dynamo.vllm` from a Dynamo runtime
image. Their explicit role templates own the launch arguments.
`$(MODELPLANE_LEADER_ADDRESS)` resolves to the leader and `$(MODELPLANE_RANK)`
to the pod's rank in its engine copy. Both load with `--load-format modelexpress`,
so they read cached weights through the shared ModelExpress server.

{{< manifests "guides/serving-multi-node-on-dynamo/model-deployment.yaml" >}}

Wait until `READY` shows `True`. The first start hydrates the cache, so it's
slower than later ones:

```bash
kubectl get md -n ml-team --watch
```

On the workload cluster, inspect the graph and the workloads its operator owns:

```bash
kubectl get dynamographdeployments.nvidia.com -A
kubectl get podcliquesets.grove.io -A
```

The graph's component counters report ready engine copies, including whole
multi-node gangs. Its Ready condition also covers the frontend and rollout.

## Expose and query

{{< manifests "guides/serving-multi-node-on-dynamo/model-service.yaml" >}}

Read the address of an InferenceGateway that serves the model and send it a
request, naming the model, `ml-team/qwen2-5-14b`, in the body:

```bash
ADDRESS=$(kubectl get ig public -o jsonpath='{.status.endpoints.openAI}')
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -s "$ADDRESS/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"ml-team/qwen2-5-14b\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}"'
```

The gateway resolves the model to the gang's one endpoint and routes the request
to its Dynamo frontend. The frontend routes inference to the engine leader.

## Scale out with peer-to-peer loading

Add a second replica and ModelExpress shows what it's for. The first replica
seeds its weights from the cache and publishes itself as a source; the second
loads them straight from the first, peer-to-peer, rather than reading the cache
again.

Each replica is a gang of two nodes. A second replica needs two more, so grow
the pool to four, then scale the deployment:

```bash
kubectl patch ic/eks-us-east-dynamo --type=json -p '[
  {"op":"replace","path":"/spec/nodePools/0/nodeCount","value":4},
  {"op":"replace","path":"/spec/nodePools/0/minNodeCount","value":4},
  {"op":"replace","path":"/spec/nodePools/0/maxNodeCount","value":4}]'
kubectl patch md/qwen2-5-14b -n ml-team --type=merge -p '{"spec":{"replicas":2}}'
```

Once the second gang starts, watch its leader load from the first over
ModelExpress:

```bash
kubectl logs -n mp-ml-team-51733 -l modelplane.ai/role=Leader -c main --tail=-1 \
  | grep "source worker"   # workload cluster, in ml-team's mirrored namespace
# [Worker 0] Trying source worker 63d91022 (266 tensors)
```
<!-- vale write-good.Passive = YES -->
