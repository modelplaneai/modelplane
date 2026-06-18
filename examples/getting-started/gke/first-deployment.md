# Part 1: Create your first inference platform and model deployment (GKE)

> GKE companion to the EKS getting-started guide. Same narrative and same small
> model — only the cloud provisioning differs. The runnable manifest for this
> part is [`01-first-deployment.yaml`](01-first-deployment.yaml).

Modelplane is an open source control plane for AI inference. It separates two
concerns: building a GPU cluster fleet with published hardware capabilities, and
deploying models against those capabilities.

In this guide you'll provision one GPU cluster on GKE and serve a small model,
then send it a request and get a response. In the [next guide](scale-to-fleet.md)
you'll add bigger clusters in other regions and let the scheduler place the model
by hardware capability — without changing cluster names or labels.

Provisioning one GPU cluster takes about 15 minutes.

## Prerequisites

You need:

* `kind`, `kubectl`, and `helm` installed on your machine.
* A GCP project with the Compute Engine and GKE APIs enabled and L4 GPU quota in
  `us-central1` (`NVIDIA_L4_GPUS >= 1`).
* A GCP service-account key (JSON) with permission to create GKE clusters, VPCs,
  and the related networking.

## Part 1: Build your inference platform

This sets up the control plane, cluster networking, and the published hardware
capabilities.

### Install the control plane

You'll run Modelplane's control plane in a local `kind` cluster. Crossplane
provides the reconciliation engine and package management.

> You can run the control plane anywhere. This guide uses `kind` for
> illustration.

```bash
kind create cluster --name modelplane
```

Install Crossplane with Helm:

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update crossplane-stable
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --set "args={--enable-dependency-version-upgrades}" \
  --wait
```

Apply the bootstrap resources (the RBAC and runtime config Crossplane needs
before it can compose anything):

```bash
kubectl apply -f ../../qwen-demo/00-prerequisites.yaml
```

### Install Modelplane

```bash
kubectl apply -f - <<'EOF'
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: modelplane
spec:
  package: xpkg.upbound.io/modelplane/modelplane:VERSION
EOF

kubectl wait configuration/modelplane --for=condition=Healthy --timeout=5m
```

### Configure cloud credentials

Create a Kubernetes secret from your GCP service-account key:

```bash
kubectl create secret generic gcp-creds \
  --from-file=credentials=/path/to/gcp-key.json \
  -n crossplane-system
```

Apply a `ClusterProviderConfig` that references it:

```bash
kubectl apply -f - <<'EOF'
apiVersion: gcp.m.upbound.io/v1beta1
kind: ClusterProviderConfig
metadata:
  name: default
spec:
  projectID: YOUR_PROJECT_ID
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: gcp-creds
      key: credentials
EOF
```

### Set up the InferenceGateway

The `InferenceGateway` installs Traefik Proxy and MetalLB on the control plane.
Traefik routes inference traffic to model replicas; MetalLB hands Traefik's
`LoadBalancer` service an external IP on `kind`, which has no cloud load
balancer. You need one per control plane, always named `default`.

If your control plane runs on a cloud cluster with native `LoadBalancer`
support, omit the `loadBalancer`/`metallb` fields.

```bash
kubectl apply -f ../../qwen-demo/01-gateway.yaml
kubectl wait --for=condition=Ready ig/default --timeout=5m
```

### Publish hardware and register the cluster

The `InferenceClass` publishes a hardware profile (machine type, accelerator,
and a DRA device with attributes and capacity). The `InferenceCluster` provisions
a GKE cluster that offers that class. Apply the first half of
[`01-first-deployment.yaml`](01-first-deployment.yaml):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gke-l4-1x-g2
spec:
  description: "GKE g2-standard-8, 1x NVIDIA L4 (the cheap starter GPU)"
  provisioning:
    provider: GKE
    gke:
      machineType: g2-standard-8
      diskSizeGb: 100
      accelerator:
        type: nvidia-l4
        count: 1
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.nvidia.com
    deviceClassName: gpu.nvidia.com
    count: 1
    attributes:
      architecture: { string: Ada Lovelace }
    capacity:
      memory: { value: "23034Mi" }
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: starter
  labels:
    modelplane.ai/region: us-central
spec:
  cluster:
    source: GKE
    gke:
      project: crossplane-playground   # set to your project
      region: us-central1
  nodePools:
  - name: gpu-l4
    className: gke-l4-1x-g2
    nodeCount: 1
    minNodeCount: 1   # keep >=1; the autoscaler can't scale a GPU pool up from 0 for DRA pods
    maxNodeCount: 2
    zones:
    - us-central1-a
```

Provisioning takes about 10–15 minutes. Wait until the cluster is `Ready`:

```bash
kubectl wait --for=condition=Ready ic/starter --timeout=20m
```

Modelplane registers the cluster and installs the serving stack. Now you're
ready to deploy a model.

## Part 2: Deploy a model

Create a namespace for the ML team's resources:

```bash
kubectl create namespace ml-team
```

Apply the `ModelDeployment` and `ModelService` (the second half of
[`01-first-deployment.yaml`](01-first-deployment.yaml)):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-demo
  namespace: ml-team
spec:
  replicas: 1
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
              device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=Qwen/Qwen2.5-0.5B-Instruct
            - --dtype=half
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: qwen
  namespace: ml-team
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen-demo
```

The device selector (`cel`) matches against the capacity declared in the
`InferenceClass`. Any L4 satisfies `>= 20Gi`, so Modelplane places the replica on
the `starter` cluster. Wait until the deployment reports one ready replica:

```bash
kubectl get md -n ml-team --watch
```

See which cluster the scheduler chose:

```bash
kubectl get modelreplica -n ml-team
```

```
NAME              CLUSTER   SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   starter   True     True    modelreplicas.modelplane.ai   12m
```

A `ModelService` selects `ModelEndpoint`s by label and creates a Gateway API
`HTTPRoute` to them. Modelplane creates one `ModelEndpoint` per replica, labeled
with the deployment name. The path is `/<namespace>/<modelservice>/...` — here
`/ml-team/qwen/`. The request body's `model` field is the model vLLM serves: the
Hugging Face id, since this deployment doesn't set `--served-model-name`.

### Send a request

```bash
QWEN=$(kubectl -n ml-team get ms qwen -o jsonpath='{.status.address}')

curl -s "$QWEN/v1/chat/completions" \
  -H "content-type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "What is Crossplane in one sentence?"}],
    "max_tokens": 100
  }' | jq -r '.choices[0].message.content'
```

You should get a one-sentence answer in a few seconds.

## Next steps

One cluster and one replica is enough to see the system work. When you're ready
to scale across a fleet and let the scheduler pick hardware by capability, the
[next guide](scale-to-fleet.md) adds clusters and edits the same deployment in
place — no model swap, no cluster names.

### Clean up (optional)

Delete the model resources before the cluster, then the cluster with foreground
cascading deletion (the serving stack must uninstall while the workload cluster's
API server is still reachable):

```bash
kubectl delete md,ms --all -n ml-team
kubectl get modelreplica -n ml-team --watch        # wait until empty
kubectl delete ic starter --cascade=foreground
kind delete cluster --name modelplane
```
