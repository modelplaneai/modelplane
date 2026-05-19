# Getting started

This guide walks through deploying Modelplane on a local kind cluster and using
it to serve a model on GKE. By the end you'll have a working OpenAI-compatible
endpoint serving Qwen 2.5 0.5B.

The whole process takes about 45 minutes. Most of that time is GKE provisioning
the GPU cluster and installing the inference stack. The GKE cluster with an L4
GPU costs roughly $2-3/hr.

## Prerequisites

You need the following tools installed:

- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/)
- The [Upbound CLI](https://docs.upbound.io/reference/cli/) (`up`), used to
  create a pull secret for the Modelplane package registry.

You also need:

- A GCP project with the GKE API enabled.
- A GCP service account key (JSON) with permissions to create GKE clusters,
  VPCs, and IAM bindings. The `Editor` role works for trying things out.

## Create a kind cluster

The control plane runs in a local kind cluster. No special configuration is
needed.

```bash
kind create cluster --name modelplane
```

## Install Crossplane

Modelplane is built on [Crossplane](https://crossplane.io) v2. Install it with
Helm:

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update crossplane-stable
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --wait
```

## Apply prerequisites

Modelplane needs a few Kubernetes resources that Crossplane can't compose for
itself: a shared namespace, RBAC for Gateway API and MetalLB resources, and a
runtime config for provider-helm.

```bash
kubectl apply -f - <<'EOF'
# Shared namespace for Modelplane infrastructure.
apiVersion: v1
kind: Namespace
metadata:
  name: modelplane-system
---
# Grant Crossplane permissions to compose Gateway API, MetalLB, and Usage
# resources. This ClusterRole is aggregated into Crossplane's role automatically.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: crossplane-compose-modelplane
  labels:
    rbac.crossplane.io/aggregate-to-crossplane: "true"
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["*"]
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: ["gateways", "gatewayclasses", "httproutes"]
    verbs: ["*"]
  - apiGroups: ["gateway.envoyproxy.io"]
    resources: ["backends"]
    verbs: ["*"]
  - apiGroups: ["metallb.io"]
    resources: ["ipaddresspools", "l2advertisements"]
    verbs: ["*"]
  - apiGroups: ["protection.crossplane.io"]
    resources: ["usages"]
    verbs: ["*"]
---
# Give provider-helm a deterministic ServiceAccount name so we can grant it
# permissions. Without this, the SA name has a random hash.
apiVersion: pkg.crossplane.io/v1beta1
kind: DeploymentRuntimeConfig
metadata:
  name: provider-helm-modelplane
spec:
  serviceAccountTemplate:
    metadata:
      name: provider-helm-modelplane
---
# Grant provider-helm cluster-admin. Helm charts install arbitrary Kubernetes
# resources and need broad permissions.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: provider-helm-modelplane
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: provider-helm-modelplane
    namespace: crossplane-system
---
# Apply the runtime config to provider-helm automatically by matching its OCI
# image prefix.
apiVersion: pkg.crossplane.io/v1beta1
kind: ImageConfig
metadata:
  name: provider-helm-modelplane
spec:
  matchImages:
    - type: Prefix
      prefix: xpkg.upbound.io/upbound/provider-helm
  runtime:
    configRef:
      name: provider-helm-modelplane
---
# Pull secret for Modelplane packages. The package registry requires
# authentication. The pull secret is applied in the next step.
apiVersion: pkg.crossplane.io/v1beta1
kind: ImageConfig
metadata:
  name: modelplane-pull-secret
spec:
  matchImages:
    - type: Prefix
      prefix: xpkg.upbound.io/modelplane/
  registry:
    authentication:
      pullSecretRef:
        name: upbound-pull-secret
EOF
```

## Install Modelplane

Modelplane is packaged as a Crossplane
[Configuration](https://docs.crossplane.io/latest/concepts/packages/#configuration-packages).
The package registry requires authentication. Create a pull secret using the
[Upbound CLI](https://docs.upbound.io/reference/cli/), then install the
Configuration. This pulls the providers and composition functions it depends on.

`up ctp pull-secret create` uses the credentials of the currently active `up`
profile. Make sure you're logged in as a user account with access to the
`modelplane` organization (run `up login` if not). Robot account profiles can
push packages but typically can't pull them, so the resulting secret won't
work.

```bash
up ctp pull-secret create -n crossplane-system upbound-pull-secret --organization modelplane
```

```bash
kubectl apply -f - <<'EOF'
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: modelplane-infra
spec:
  package: xpkg.upbound.io/modelplane/modelplane:v0.1.0-dev.125.g0cba874
EOF
```

Wait for the Configuration and all its dependencies to become healthy. This
pulls several container images and takes 5-10 minutes.

```bash
kubectl get configuration modelplane-infra --watch
# Wait until HEALTHY shows True, then Ctrl-C.
```

## Configure GCP credentials

Create a Secret with your GCP service account key, then create a ProviderConfig
that references it.

```bash
kubectl create secret generic gcp-creds \
  --from-file=credentials=/path/to/sa-key.json \
  -n crossplane-system
```

```bash
kubectl apply -f - <<'EOF'
apiVersion: gcp.m.upbound.io/v1beta1
kind: ClusterProviderConfig
metadata:
  name: default
spec:
  projectID: my-gcp-project  # Replace with your GCP project ID.
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: gcp-creds
      key: credentials
EOF
```

## Create the InferenceGateway

The InferenceGateway installs Envoy Gateway and MetalLB on the control plane
cluster and creates a Gateway that routes traffic to model endpoints.

```bash
kubectl apply -f examples/platform/inference-gateway.yaml
```

Wait for it to become ready (~3-5 minutes):

```bash
kubectl get ig default --watch
```

## Create an InferenceClass and InferenceCluster

An InferenceClass defines a hardware recipe (GPU type, count, provisioning
config). An InferenceCluster references it to provision GPU node pools.

Apply the L4 InferenceClass, then edit the cluster example to set your GCP
project ID and apply it:

```bash
kubectl apply -f examples/platform/inference-class-gke-l4.yaml
```

```bash
# Edit examples/platform/inference-cluster-gke.yaml and set
# spec.cluster.gke.project to your GCP project ID.
kubectl apply -f examples/platform/inference-cluster-gke.yaml
```

This provisions a GKE cluster with an L4 GPU and installs the inference stack.
It's the longest step, taking roughly 20-30 minutes.

```bash
kubectl get ic --watch
# Wait until READY shows True, then Ctrl-C.
```

## Deploy a model

Create the `ml-team` namespace, deploy the model, and create a ModelService to
expose it:

```bash
kubectl create namespace ml-team
kubectl apply -f examples/deployment/model-deployment.yaml
kubectl apply -f examples/deployment/model-service.yaml
```

The scheduler matches the deployment's topology against the cluster's GPU
capacity and creates a ModelReplica. Wait for the deployment to become ready:

```bash
kubectl get md -n ml-team --watch
# Wait until REPLICAS shows 1, then Ctrl-C.
```

## Talk to the model

The gateway endpoint is only reachable from inside the kind Docker network.
Use a pod to send a request:

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://172.18.255.200/ml-team/qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "What is Crossplane in one sentence?"}],
    "max_tokens": 100
  }'
```

You can also get the endpoint URL from the ModelService status:

```bash
kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}'
```

## Clean up

Delete the ModelDeployment before the InferenceCluster. If you delete the
cluster first, the deployment will be stuck trying to reconcile against a
cluster that's being torn down.

Wait for the GKE cluster to be fully deprovisioned before deleting the kind
cluster. If you delete the kind cluster while Crossplane is still cleaning up,
the GKE resources will be orphaned.

```bash
kubectl delete md --all -n ml-team
kubectl delete ms --all -n ml-team
kubectl delete ic --all

# Wait for the InferenceCluster to be fully deleted.
kubectl get ic --watch
# Wait until no resources remain, then Ctrl-C.

kind delete cluster --name modelplane
```
