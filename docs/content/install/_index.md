---
title: Install
weight: 8
navLanding: "Install the control plane"
description: Stand up the Modelplane control plane on a Kubernetes cluster you run.
---
Modelplane's control plane is where everything runs: the Crossplane runtime, the
providers it provisions infrastructure through, and the composition functions
that reconcile the Modelplane APIs. You install it on a Kubernetes cluster that
becomes the control cluster for your inference fleet.

The control cluster runs Modelplane itself, not model workloads, so it needs no
GPUs.

## Requirements

- **A maintained Kubernetes version.** [Any release the Kubernetes project still
  supports](https://kubernetes.io/releases/) works. Modelplane doesn't pin the
  control cluster's Kubernetes version.
- **Helm and [kubectl](https://kubernetes.io/docs/tasks/tools/)**, to install
  Modelplane and reach the cluster.
- **Room for Modelplane's components.** At rest they use a fraction of a CPU core
  and around 1.5 GiB of memory, split across Crossplane, its providers, and the
  composition functions. This grows as Modelplane provisions clusters and more
  providers activate, so give the control cluster a few GiB of headroom beyond
  what your Kubernetes distribution needs.

{{< hint "important" >}}
You can run the control plane anywhere. To try it locally, create a
[kind](https://kind.sigs.k8s.io/) cluster:

```bash
# Pin to kind v0.30.0 default image (containerd 2.1.4)
# kind v0.31+ ships containerd 2.2.0 which breaks Modelplane
kind create cluster --name modelplane \
  --image kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a
```

Give your container engine room for it. On Docker Desktop, raise the memory limit
to **8 GB** (see the [Docker
documentation](https://docs.docker.com/desktop/settings-and-maintenance/settings/#advanced)).
{{< /hint >}}

## Install Crossplane

Crossplane provides Modelplane's reconciliation engine and package management.
Modelplane needs Crossplane v2.3 or newer. Install it with Helm:

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update crossplane-stable
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --set "args={--enable-dependency-version-upgrades}" \
  --set-json 'provider.defaultActivations=[]' \
  --wait
```

Apply the bootstrap resources. They grant Crossplane the permissions it needs to
manage your cluster:

```shell
kubectl apply -f {{< manifest-url "install/prerequisites.yaml" >}}
```

{{< expand "Review the prerequisites manifest" >}}
{{< manifests "install/prerequisites.yaml" >}}
{{< /expand >}}

## Install Modelplane

The Modelplane Configuration adds the Modelplane APIs and the composition
functions that reconcile them:

{{< manifests "install/configuration.yaml" >}}

Wait until the configuration is healthy:

```bash
kubectl wait configuration/modelplane --for=condition=Healthy --timeout=5m
```

## Next step

With the control plane running, [take the tour]({{< ref "/getting-started" >}}) to
provision a GPU cluster and serve a model, or [register a cluster]({{< ref
"platform/inference-cluster.md" >}}) you already run.
