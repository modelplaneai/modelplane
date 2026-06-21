---
title: Scale the platform
weight: 40
description: Grow from one cluster to a multi-region fleet.
---
Back to the platform team. You have one L4 cluster so far; here you add two
larger-GPU clusters in different regions, growing the fleet the ML team can
schedule against.

Provisioning two more clusters takes about 10–15 minutes.

## Register more clusters

{{< tabs >}}
{{< tab "EKS" >}}
Register two more clusters with a bigger hardware class: `L40S` (`48 Gi`) in
`us-west` and `eu-central`:

{{< manifests "getting-started/eks/platform-scale.yaml" >}}

{{< hint "note" >}}
`g6e.xlarge` runs ~$2/hr on demand. Two of them plus the `L4` from earlier is a
few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "GKE" >}}
Register two more clusters with a bigger hardware class: `A100` (`40 Gi`) in
`us-west` and `us-east`. Apply the manifest, setting each cluster's `project` to
your GCP project:

{{< manifests path="getting-started/gke/platform-scale.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "getting-started/gke/platform-scale.yaml" >}} \
  | sed 's/my-gcp-project/$@<your-gcp-project>$@/g' \
  | kubectl apply -f -
```
{{< /editCode >}}

{{< hint "note" >}}
`a2-highgpu-1g` runs ~$3.50/hr on demand. Two of them plus the `L4` from earlier
is a few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< /tabs >}}

Modelplane provisions both clusters in parallel:

```bash
kubectl wait --for=condition=Ready ic --all --timeout=20m
```

## Your model keeps running

Growing the fleet doesn't disturb anything already deployed. Your `qwen-demo`
replica stays on its original cluster; the new clusters add capacity, available
the moment they're `Ready`. A replica only moves if its
deployment changes in a way that no longer fits where it runs.

## Next step

The fleet now spans multiple regions. Switch back to the ML team and [scale the
model]({{< ref "getting-started/scale-the-model.md" >}}) to serve it from two of
them behind a single endpoint.
