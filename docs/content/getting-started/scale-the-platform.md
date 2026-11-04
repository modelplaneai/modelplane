---
title: Scale the platform
weight: 40
description: Grow from one cluster to a multi-region fleet.
---

You have one small-GPU cluster with a running model. In this guide, you'll grow
the fleet with larger-GPU capacity so the ML team has more to schedule against.

Provisioning takes about 10 to 15 minutes.

## Register more clusters

{{< tabs >}}
{{< tab "EKS" >}}
Register two more clusters with a bigger hardware class: `L40S` (`48 GB`) in
`us-west` and `eu-central`:

{{< manifests "getting-started/eks/platform-scale.yaml" >}}

{{< hint "note" >}}
`g6e.xlarge` runs ~$2/hr on demand. Two of them plus the `L4` from earlier is a
few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "GKE" >}}
Register two more clusters with a bigger hardware class: `A100` (`40 GB`) in
`us-west` and `us-east`. Apply the manifest:

{{< manifests path="getting-started/gke/platform-scale.yaml" apply="false" >}}

```bash
kubectl apply -f {{< manifest-url "getting-started/gke/platform-scale.yaml" >}}
```

{{< hint "note" >}}
`a2-highgpu-1g` runs ~$3.50/hr on demand. Two of them plus the `L4` from earlier
is a few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "AKS" >}}
Register two more clusters with a bigger hardware class: `A100` (`80 GB`) in
`eastus` and `southcentralus`:

{{< manifests "getting-started/aks/platform-scale.yaml" >}}

{{< hint "note" >}}
`Standard_NC24ads_A100_v4` runs ~$3.70/hr on demand. Two of them plus the `A10`
from earlier is a few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "Nebius" >}}
Nebius projects are bound to one region, so you grow the fleet by GPU tier rather
than geography. Register a bigger `H100` (`80 GB`) cluster in the same region:

{{< manifests "getting-started/nebius/platform-scale.yaml" >}}

{{< hint "note" >}}
The `H100` cluster costs more per hour than the `L40S` from earlier. Clean up
when you're done (see [Clean up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "Vultr" >}}
Vultr GPU plans are region-gated: `ewr` offers only `A16` plans, and the
`A40` is sold in `lax` and `syd`. Register a cluster with the bigger
hardware class, `A40` (`48 GB`), in `lax`:

{{< manifests "getting-started/vultr/platform-scale.yaml" >}}

{{< hint "note" >}}
`vcg-a40-24c-120g-48vram` runs ~$1.70/hr on demand. It plus the `A16` from
earlier is a few dollars for this tour. Clean up when you're done (see [Clean
up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< tab "Civo" >}}
Civo GPU sizes are region-gated: `NYC1` offers `L40S` sizes, and `LON1`
offers the `A100` and `H100`. Register a cluster with the bigger hardware
class, `A100` (`80 GB`), in `LON1`:

{{< manifests "getting-started/civo/platform-scale.yaml" >}}

{{< hint "note" >}}
The `A100` cluster costs more per hour than the `L40S` from earlier. Clean up
when you're done (see [Clean up]({{< ref "getting-started/clean-up.md" >}})).
{{< /hint >}}
{{< /tab >}}
{{< /tabs >}}

Modelplane provisions the new clusters in parallel:

```bash
kubectl wait --for=condition=Ready ic --all --timeout=20m
```

## Your model keeps running

Growing the fleet doesn't disturb anything already deployed. `qwen-demo` stays
on its original cluster and the new capacity comes online the moment it's
`Ready` with no interruption for the ML team. A replica only moves if
its deployment changes in a way that no longer fits where it runs. 

## Next step

The fleet has grown with larger-GPU capacity. The ML team is next. [Scale the model]({{< ref "getting-started/scale-the-model.md" >}}) to serve it across the fleet behind one endpoint.
