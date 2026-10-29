---
title: Build the platform
weight: 20
description: Give the control plane access to your infrastructure, register your first GPU cluster, and set up the gateway.
---
This is the platform team's side of Modelplane. You set up the gateway that
fronts your models, give the control plane access to where models will run,
and register your first GPU cluster: a hardware profile published as an
`InferenceClass` and an `InferenceCluster` that offers it.

In the next step, the ML team will create a model deployment that schedules
against this capacity without knowing which cluster it runs on.

## Prerequisites

{{< tabs >}}
{{< tab "EKS" >}}
- An AWS account with permissions to create EKS clusters, VPCs, and IAM roles
- AWS access key ID and secret access key
{{< /tab >}}
{{< tab "GKE" >}}
- A GCP service account JSON key, granted these roles on the project:

  | Role | Needed for |
  |---|---|
  | `roles/container.admin` | the cluster and its node pools |
  | `roles/compute.admin` | the VPC network and subnet |
  | `roles/serviceusage.serviceUsageAdmin` | enabling the APIs the cluster needs |
  | `roles/iam.serviceAccountAdmin` | the node service account |
  | `roles/iam.serviceAccountKeyAdmin` | the node service account's key |
  | `roles/iam.serviceAccountUser` | attaching that account to the nodes |
  | `roles/resourcemanager.projectIamAdmin` | granting the node account `container.admin` |

  Review the last one before you hand the key over. Modelplane grants the node
  service account `roles/container.admin`, so the credential doing the
  provisioning has to be able to set project IAM policy.
{{< /tab >}}
{{< tab "AKS" >}}
- An Azure account with permissions to create AKS clusters and managed identities
- An Azure service principal JSON with `clientId`, `clientSecret`, `subscriptionId`, and `tenantId`
{{< /tab >}}
{{< tab "Nebius" >}}
- A Nebius account with permissions to create clusters
- A Nebius service account JSON key and your project ID
{{< /tab >}}
{{< tab "Vultr" >}}
- A Vultr account with access to GPU plans
- A Vultr API key
{{< /tab >}}
{{< tab "Civo" >}}
- A Civo account with access to GPU node sizes
- A Civo API key
{{< /tab >}}
{{< tab "kind" >}}
- No cloud account and no GPU
- Room in your container engine for a second kind cluster and the serving stack
  Modelplane installs on it. On Docker Desktop, raise the memory limit from the
  8 GB the control plane needs to **12 GB**.
{{< /tab >}}
{{< /tabs >}}

## Give the control plane access

Give the control plane what it needs to reach the cluster models will run on:
credentials to provision one in your cloud account, or a kubeconfig for a
cluster you run yourself.

{{<tabs>}}
{{< tab "EKS" >}}
Create an AWS credentials file:

{{< editCode >}}
```ini
[default]
aws_access_key_id = $@<aws_access_key>$@
aws_secret_access_key = $@<aws_secret_key>$@
```
{{< /editCode >}}

Create a Kubernetes secret:

{{< editCode >}}
```bash
kubectl create secret generic aws-creds \
  --from-file=credentials=$@</path/to/aws-credentials>$@ \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig` referencing your secret:

{{< manifests "getting-started/clusterproviderconfig-aws.yaml" >}}
{{< /tab >}}

{{<tab "GKE" >}}
Create a Kubernetes secret:

{{< editCode >}}
```bash
kubectl create secret generic gcp-creds \
  --from-file=credentials=$@<path/to/gcp-key>$@.json \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig`, setting `projectID` to your GCP project:

{{< manifests path="getting-started/clusterproviderconfig-gke.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "getting-started/clusterproviderconfig-gke.yaml" >}} \
  | sed 's/my-gcp-project/$@<your-gcp-project>$@/' \
  | kubectl apply -f -
```
{{< /editCode >}}
{{< /tab >}}

{{< tab "AKS" >}}
Create a Kubernetes secret from your service principal JSON:

{{< editCode >}}
```bash
kubectl create secret generic azure-credentials \
  --from-file=credentials.json=$@</path/to/azure-sp>$@.json \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig` referencing your secret:

{{< manifests "getting-started/clusterproviderconfig-azure.yaml" >}}
{{< /tab >}}

{{< tab "Nebius" >}}
Create a Kubernetes secret from your service account JSON:

{{< editCode >}}
```bash
kubectl create secret generic nebius-credentials \
  --from-file=credentials.json=$@</path/to/nebius-sa>$@.json \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig`, setting `projectID` to your Nebius project:

{{< manifests path="getting-started/clusterproviderconfig-nebius.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "getting-started/clusterproviderconfig-nebius.yaml" >}} \
  | sed 's/project-e00example/$@<your-nebius-project>$@/' \
  | kubectl apply -f -
```
{{< /editCode >}}
{{< /tab >}}

{{< tab "Vultr" >}}
Create a Kubernetes secret from your API key:

{{< editCode >}}
```bash
kubectl create secret generic vultr-credentials \
  --from-literal=api-key=$@<vultr-api-key>$@ \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig` referencing your secret:

{{< manifests "getting-started/clusterproviderconfig-vultr.yaml" >}}
{{< /tab >}}

{{< tab "Civo" >}}
Create a Kubernetes secret from your API key, as the JSON `creds` key
the provider parses:

{{< editCode >}}
```bash
kubectl create secret generic civo-credentials \
  --from-literal=creds='{"token": "$@<civo-api-key>$@"}' \
  -n crossplane-system
```
{{< /editCode >}}

Apply the `ClusterProviderConfig` referencing your secret:

{{< manifests "getting-started/clusterproviderconfig-civo.yaml" >}}
{{< /tab >}}

{{< tab "kind" >}}
You don't configure credentials here. Instead you create a second kind cluster
for Modelplane to deploy into, then give the control plane a kubeconfig for it.

Create the cluster, on the same image as the control plane. Creating it switches
your current context to it. The commands up to the `Secret` run against it:

```bash
kind create cluster --name modelplane-workload \
  --image kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
```

The gateway and the serving stack need `LoadBalancer` addresses, which kind
doesn't provide. Install MetalLB, and give it a pool inside the Docker network
kind runs on so the control plane can reach the addresses it hands out:

```bash
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml
kubectl -n metallb-system rollout status deploy/controller --timeout=180s
PREFIX=$(docker network inspect kind -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' \
  | grep -E '^[0-9]+\.' | head -1 | cut -d. -f1-2)
kubectl apply -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: kind-pool, namespace: metallb-system }
spec: { addresses: ["${PREFIX}.255.100-${PREFIX}.255.149"] }
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: kind-l2, namespace: metallb-system }
spec: { ipAddressPools: [kind-pool] }
EOF
```

Install the [`dra-example-driver`](https://github.com/kubernetes-sigs/dra-example-driver),
which publishes fake GPU devices for Modelplane to schedule against, and label
the node for the pool you register in the next section. Modelplane doesn't
label the nodes of a cluster it didn't provision:

```bash
kubectl apply -f {{< manifest-url "getting-started/kind/dra-example-driver.yaml" >}}
kubectl label node modelplane-workload-control-plane modelplane.ai/pool=gpu-synthetic
```

{{< hint "note" >}}
This tab uses a fake GPU rather than a Modelplane CPU pool. CPU pools publish
a node's cores through `dra-driver-cpu`, which needs a kernel with NUMA support,
and Docker Desktop's doesn't have one.
{{< /hint >}}

Switch back to the control plane, and store the cluster's kubeconfig there in a
`Secret`. `--internal` gives the address on the kind network, which the control
plane's pods can reach:

```bash
kubectl config use-context kind-modelplane
kubectl create secret generic local-cluster-kubeconfig --namespace modelplane-system \
  --from-literal=kubeconfig="$(kind get kubeconfig --internal --name modelplane-workload)"
```
{{< /tab >}}
{{</tabs>}}

## Publish hardware and register the cluster

The `InferenceClass` describes a hardware profile and how to provision it. The
`InferenceCluster` registers a cluster that offers it. Apply both:

{{< tabs >}}
{{< tab "EKS">}}
{{< manifests "getting-started/eks/platform.yaml" >}}

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/eks-us-east --timeout=20m
```
{{< /tab >}}

{{< tab "GKE" >}}
Apply the manifest:

{{< manifests path="getting-started/gke/platform.yaml" apply="false" >}}

```bash
kubectl apply -f {{< manifest-url "getting-started/gke/platform.yaml" >}}
```

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/gke-us-central --timeout=20m
```
{{< /tab >}}

{{< tab "AKS" >}}
{{< manifests "getting-started/aks/platform.yaml" >}}

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/aks-westeurope --timeout=20m
```
{{< /tab >}}

{{< tab "Nebius" >}}
{{< manifests "getting-started/nebius/platform.yaml" >}}

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/nebius-eu-north --timeout=20m
```
{{< /tab >}}

{{< tab "Vultr" >}}
{{< manifests "getting-started/vultr/platform.yaml" >}}

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/vultr-ewr --timeout=20m
```
{{< /tab >}}

{{< tab "Civo" >}}
{{< manifests "getting-started/civo/platform.yaml" >}}

Modelplane provisions the cluster. This takes about 15 minutes (Civo GPU
node images don't include the NVIDIA driver, so the inference stack's
GPU Operator installs it on first boot):

```bash
kubectl wait --for=condition=Ready ic/civo-nyc1 --timeout=20m
```
{{< /tab >}}

{{< tab "kind" >}}
{{< manifests "getting-started/kind/platform.yaml" >}}

Modelplane installs the serving stack on your cluster. This takes a few
minutes:

```bash
kubectl wait --for=condition=Ready ic/local --timeout=10m
```
{{< /tab >}}
{{< /tabs >}}

{{< hint "note" >}}
Modelplane is reconciling the infrastructure against the source of truth, the
manifest you just applied.

While you wait, Modelplane is creating the cloud cluster and its GPU node
pool, then installing the inference stack with LeaderWorkerSet for multi-node
serving (the default; a cluster can opt into Grove and KAI Scheduler instead via
`InferenceCluster.spec.stack: Dynamo`), llm-d for inference-aware routing,
Envoy Gateway for traffic management, and the storage class for model weights.
On a cluster you run yourself, it skips creating the cluster and installs the
same stack. This is the same reconciliation loop Crossplane uses to configure other
infrastructure, extended to the inference layer.
{{< /hint >}}

{{< hint "note" >}}
A cloud GPU cluster costs money while it runs. To stop the tour and resume
later, follow [Clean up]({{< ref "getting-started/clean-up.md" >}}). The kind
path doesn't cost anything, but keeps two clusters running on your machine.
{{< /hint >}}

## Set up the InferenceGateway

<!-- vale ai-tells.EmptyPadding = NO -->
The `InferenceGateway` is the address callers reach your models through. It
speaks the OpenAI and Anthropic APIs, authenticates callers, and resolves the
model a request names to a `ModelService`.
<!-- vale ai-tells.EmptyPadding = YES -->

It runs on an `InferenceCluster`, named by `spec.clusterName`
Each cloud's manifest names the cluster you just registered, so pick your
cloud's tab.

This one is the smallest useful shape: it answers on its address over plain HTTP
and doesn't authenticate callers. See
[Set Up the Gateway]({{< ref "/platform/inference-gateway" >}})
for an example with TLS and authentication.

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "getting-started/eks/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "getting-started/gke/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "AKS" >}}
{{< manifests "getting-started/aks/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "Nebius" >}}
{{< manifests "getting-started/nebius/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "Vultr" >}}
{{< manifests "getting-started/vultr/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "Civo" >}}
{{< manifests "getting-started/civo/inference-gateway.yaml" >}}
{{< /tab >}}
{{< tab "kind" >}}
{{< manifests "getting-started/kind/inference-gateway.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

Wait until the gateway is ready:

```bash
kubectl wait --for=condition=Ready ig/public --timeout=5m
```

With the cluster registered and a gateway in front of it, the ML team can deploy
a model.

## Next step

Now that the platform is provisioned, the ML team can [deploy a model]({{< ref
"getting-started/deploying-a-model.md" >}}) by describing what the model needs, not the infrastructure.
