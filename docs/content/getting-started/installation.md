---
title: Installation
weight: 10
description: Stand up the Modelplane control plane on a local kind cluster.
---
The control plane is where everything in Modelplane runs. In this step you'll
install it on a local kind cluster: Crossplane for reconciliation and the
Modelplane APIs. No cloud yet, that comes next.

This step takes about five minutes.

## Prerequisites

Install [kind](https://kind.sigs.k8s.io/),
[kubectl](https://kubernetes.io/docs/tasks/tools/), and
[Helm](https://helm.sh/docs/intro/install/) on your machine.

{{< hint "note" >}}
You can run your Modelplane control plane anywhere. This tour uses kind for
illustration.
{{< /hint >}}

## Install the control plane

Crossplane provides the reconciliation engine and package management. Create the
kind cluster and install it with Helm:

```bash
kind create cluster --name modelplane
```

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update crossplane-stable
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --set "args={--enable-dependency-version-upgrades}" \
  --wait
```

Apply the bootstrap resources. They grant Crossplane the permissions it needs to
manage your cluster:

```shell
kubectl apply -f {{< manifest-url "getting-started/prerequisites.yaml" >}}
```

{{< expand "Review the prerequisites manifest" >}}
{{< manifests "getting-started/prerequisites.yaml" >}}
{{< /expand >}}

## Install Modelplane

The Modelplane Configuration adds the Modelplane APIs and the composition
functions that reconcile them:

{{< manifests "getting-started/configuration.yaml" >}}

Wait until the configuration is healthy:

```bash
kubectl wait configuration/modelplane --for=condition=Healthy --timeout=5m
```

## Next step

Your control plane is running, but it has no hardware to schedule against yet.
Next, [build the platform]({{< ref "getting-started/build-the-platform.md" >}}):
register a GPU cluster and set up the gateway that fronts your models.
