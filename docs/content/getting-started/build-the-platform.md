---
title: Build the platform
weight: 20
description: Set up the gateway, give the control plane cloud credentials, and provision your first GPU cluster.
---
This is the platform team's side of Modelplane. You set up the gateway that
fronts your models, give the control plane cloud credentials, and register your
first GPU cluster: a hardware profile published as an `InferenceClass` and an
`InferenceCluster` that offers it. Models scheduled later match against the
capacity you declare here.

Provisioning one GPU cluster takes about 15 minutes.

## Prerequisites

{{< tabs >}}
{{< tab "EKS" >}}
- An AWS account with permissions to create EKS clusters, VPCs, and IAM roles
- AWS access key ID and secret access key
{{< /tab >}}
{{< tab "GKE" >}}
- A GCP account with permissions to create GKE clusters, VPCs, and IAM roles
- A GCP service account JSON key
{{< /tab >}}
{{< /tabs >}}

## Set up the InferenceGateway

The `InferenceGateway` installs Traefik Proxy and MetalLB on the control plane.
Traefik routes inference traffic to model replicas. MetalLB assigns Traefik's
`LoadBalancer` service an external IP on kind, which doesn't have a cloud load
balancer. You need one per control plane, always named `default`.

If you run the control plane on a cloud cluster with native `LoadBalancer`
support, omit the `loadBalancer` field.

{{< manifests "getting-started/inference-gateway.yaml" >}}

Wait until the gateway is ready:

```bash
kubectl wait --for=condition=Ready ig/default --timeout=5m
```

## Configure cloud credentials

Give the control plane credentials so it can provision clusters in your cloud
account.

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
Apply the manifest, setting the cluster's `project` to your GCP project:

{{< manifests path="getting-started/gke/platform.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "getting-started/gke/platform.yaml" >}} \
  | sed 's/my-gcp-project/$@<your-gcp-project>$@/' \
  | kubectl apply -f -
```
{{< /editCode >}}

Modelplane provisions the cluster. This takes about 15 minutes:

```bash
kubectl wait --for=condition=Ready ic/starter --timeout=20m
```
{{< /tab >}}
{{< /tabs >}}

Once the cluster is `Ready`, Modelplane has registered it and installed the
serving stack.

{{< hint "note" >}}
A cloud GPU cluster costs money while it runs. To stop the tour and resume
later, follow [Clean up]({{< ref "getting-started/clean-up.md" >}}).
{{< /hint >}}

## Next step

You have a cluster with published hardware. Now switch hats to the ML team and
[deploy a model]({{< ref "getting-started/deploying-a-model.md" >}}) onto it.
