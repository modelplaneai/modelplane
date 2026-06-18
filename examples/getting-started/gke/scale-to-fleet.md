# Part 2: Scale to a multi-cluster fleet (GKE)

> GKE companion to the EKS getting-started guide. The runnable manifest for this
> part is [`02-scale-to-fleet.yaml`](02-scale-to-fleet.yaml). It continues from
> [Part 1](first-deployment.md) — you need the control plane and the `starter`
> cluster running.

By the end, one `ModelDeployment` runs replicas across two A100 clusters in
different regions, routed through the same endpoint as Part 1. The L4 starter is
still there but skipped — the CEL device selector targets hardware capability,
not cluster identity.

Provisioning two clusters takes about 10–15 minutes.

## Part 1: Scale your inference fleet

Register a bigger hardware class (1x A100 40GB) and two clusters that offer it,
in different regions. Apply the first half of
[`02-scale-to-fleet.yaml`](02-scale-to-fleet.yaml):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gke-a100-40-1x
spec:
  description: "GKE a2-highgpu-1g, 1x NVIDIA A100 40GB"
  provisioning:
    provider: GKE
    gke:
      machineType: a2-highgpu-1g
      diskSizeGb: 200
      accelerator:
        type: nvidia-tesla-a100
        count: 1
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.nvidia.com
    deviceClassName: gpu.nvidia.com
    count: 1
    attributes:
      architecture: { string: Ampere }
      cudaComputeCapability: { version: "8.0.0" }
    capacity:
      memory: { value: "40960Mi" }
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: gpu-us-west
  labels:
    modelplane.ai/region: us-west
spec:
  cluster:
    source: GKE
    gke:
      project: crossplane-playground   # set to your project
      region: us-west1
  nodePools:
  - name: gpu-a100
    className: gke-a100-40-1x
    nodeCount: 1
    minNodeCount: 1   # keep >=1; the autoscaler can't scale a GPU pool up from 0 for DRA pods
    maxNodeCount: 2
    zones:
    - us-west1-b
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: gpu-us-east
  labels:
    modelplane.ai/region: us-east
spec:
  cluster:
    source: GKE
    gke:
      project: crossplane-playground
      region: us-east1
  nodePools:
  - name: gpu-a100
    className: gke-a100-40-1x
    nodeCount: 1
    minNodeCount: 1   # keep >=1; the autoscaler can't scale a GPU pool up from 0 for DRA pods
    maxNodeCount: 2
    zones:
    - us-east1-b
```

> **GPU choice:** A100 40GB (`a2-highgpu-1g`) is a single-GPU node. This project
> has A100-40 quota but zero A100-80/H100 quota, and GKE has no 1-GPU H100 node
> (`a3-highgpu-8g` is 8x). For A100 80GB, use `a2-ultragpu-1g` /
> `nvidia-a100-80gb` and raise the selector to `>= 70Gi`.

Wait until both clusters are `Ready`:

```bash
kubectl wait --for=condition=Ready ic --all --timeout=20m
```

## Part 2: Request new hardware for your model

The ML team doesn't swap models or name clusters. It edits the **same**
`qwen-demo` deployment in place: more replicas, and a selector that asks for more
GPU memory. The DRA scheduler finds it fleet-wide.

```
            ┌─ starter         L4   · 24Gi ─┐  24Gi — not matched
 memory     ├─ gpu-us-west     A100 · 40Gi ─┤  40Gi ✓
 >= 35Gi    └─ gpu-us-east     A100 · 40Gi ─┘  40Gi ✓
```

Apply the second half of [`02-scale-to-fleet.yaml`](02-scale-to-fleet.yaml):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-demo
  namespace: ml-team
spec:
  replicas: 2
  engines:
  - name: qwen
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: |
              device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("35Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=Qwen/Qwen2.5-0.5B-Instruct
            - --dtype=half
```

> The 0.5B model doesn't need 40 GB — this shows the selection mechanism. Size
> the threshold to your model. Bump it to `>= 35Gi` and the L4 (24 GB) no longer
> qualifies, so the replicas move to the A100 clusters.

Wait until the deployment reports two ready replicas:

```bash
kubectl get md -n ml-team --watch
```

Check placement:

```bash
kubectl get modelreplica -n ml-team
```

```
NAME              CLUSTER          SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   gpu-us-west      True     True    modelreplicas.modelplane.ai    8m
qwen-demo-92535   gpu-us-east      True     True    modelreplicas.modelplane.ai    8m
```

The endpoint URL doesn't change. The same `qwen` `ModelService` from Part 1
picks up the new replicas, so the request from Part 1 works unchanged — now
served from the A100 clusters:

```bash
QWEN=$(kubectl -n ml-team get ms qwen -o jsonpath='{.status.address}')
curl -s "$QWEN/v1/chat/completions" \
  -H "content-type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Reverse a linked list in Python:"}],"max_tokens":100}' \
  | jq -r '.choices[0].message.content'
```

Any new A100 cluster that becomes `Ready` is eligible automatically — no changes
to the `ModelDeployment`.

## Clean up

Delete model resources before clusters, then the clusters with foreground
cascading deletion (each serving stack must uninstall while its workload
cluster's API server is reachable):

```bash
kubectl delete md,ms --all -n ml-team
kubectl get modelreplica -n ml-team --watch        # wait until empty
kubectl delete ic --all --cascade=foreground
kind delete cluster --name modelplane
```

## Next steps

* [InferenceClass](../../../docs/content/platform/inference-class.md)
* [InferenceCluster](../../../docs/content/platform/inference-cluster.md)
* [ModelDeployment](../../../docs/content/models/model-deployment.md)

Star the [Modelplane project on GitHub](https://github.com/modelplaneai/modelplane)
and build with us.
