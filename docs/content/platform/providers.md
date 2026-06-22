---
title: Supported Providers
weight: 40
description: The clouds and neoclouds Modelplane runs on today, and the Crossplane providers it grows into.
---
Modelplane is built on [Crossplane](https://crossplane.io) and shares its
infrastructure providers, so the set of clouds and neoclouds it reaches grows
alongside Crossplane itself. This page shows where Modelplane runs today and
where it's headed.

A provider can show up here in three ways:

{{< hint "note" >}}
- **Provisioning supported.** Modelplane creates and manages the whole cluster
  from an `InferenceCluster`, selected through `provisioning.provider`. GKE and
  EKS work this way today.
- **Bring your own supported.** Register a cluster you already run with
  `source: Existing`. This works on any provider whose Kubernetes meets
  Modelplane's requirements (Dynamic Resource Allocation and a recent Kubernetes
  version), so you can run on the clouds below now, ahead of native
  provisioning.
- **Crossplane provider exists.** A Crossplane provider is published for the
  cloud. That provider is the path by which native provisioning lands, so it
  marks where Modelplane can grow next.
{{< /hint >}}

## Clouds

Listed alphabetically. Each runs a managed Kubernetes service with GPU node
pools, and most have a Crossplane provider, the path to native provisioning.

<!-- vale Vale.Terms = NO -->
<!-- vale Modelplane.Spelling = NO -->
{{< table >}}
| Cloud / service | Accelerators | Provisioning | Bring your own | Crossplane provider |
|---|---|---|---|---|
| Alibaba Cloud (ACK) | {{< accel nvidia >}} | Planned | ✓ | [provider-upjet-alibabacloud](https://github.com/crossplane-contrib/provider-upjet-alibabacloud) |
| AWS (EKS) | {{< accel nvidia >}} {{< accel trainium >}} | ✓ | ✓ | [provider-upjet-aws](https://github.com/crossplane-contrib/provider-upjet-aws) |
| Civo (K3s) | {{< accel nvidia >}} | Planned | ✓ | [provider-civo](https://github.com/crossplane-contrib/provider-civo) (community) |
| DigitalOcean (DOKS) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | [provider-upjet-digitalocean](https://github.com/crossplane-contrib/provider-upjet-digitalocean) |
| Google Cloud (GKE) | {{< accel nvidia >}} {{< accel tpu >}} | ✓ | ✓ | [provider-upjet-gcp](https://github.com/crossplane-contrib/provider-upjet-gcp) |
| Huawei Cloud (CCE) | {{< accel nvidia >}} {{< accel ascend >}} | Planned | ✓ | [provider-huaweicloud](https://github.com/huaweicloud/provider-huaweicloud) (alpha) |
| IBM Cloud (IKS) | {{< accel nvidia >}} | Planned | ✓ | none active |
| Linode / Akamai (LKE) | {{< accel nvidia >}} | Planned | ✓ | [provider-linode](https://github.com/linode/provider-linode) (official) |
| Microsoft Azure (AKS) | {{< accel nvidia >}} | Planned | ✓ | [provider-upjet-azure](https://github.com/crossplane-contrib/provider-upjet-azure) |
| Oracle Cloud (OKE) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | [crossplane-provider-oci](https://github.com/oracle/crossplane-provider-oci) (official) |
| Tencent Cloud (TKE) | {{< accel nvidia >}} | Planned | ✓ | [provider-tencentcloud](https://github.com/crossplane-contrib/provider-tencentcloud) |
{{< /table >}}
<!-- vale Modelplane.Spelling = YES -->
<!-- vale Vale.Terms = YES -->

## Neoclouds

GPU-specialist clouds, listed alphabetically. The ones below run a managed
Kubernetes service, so the bring-your-own path already covers them today. Most
have no Crossplane provider yet; where one exists, it points the way to native
provisioning.

<!-- vale Vale.Terms = NO -->
<!-- vale Modelplane.Spelling = NO -->
{{< table >}}
| Neocloud / service | Accelerators | Provisioning | Bring your own | Crossplane provider |
|---|---|---|---|---|
| CoreWeave (CKS) | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Crusoe (CMK) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | none yet |
| Fluidstack (Managed Kubernetes) | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Lambda (Managed Kubernetes) | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Nebius (Managed Kubernetes) | {{< accel nvidia >}} | Planned | ✓ | none yet |
| OVHcloud (Managed Kubernetes) | {{< accel nvidia >}} | Planned | ✓ | [edixos/provider-ovh](https://github.com/edixos/provider-ovh) (community) |
| Scaleway (Kapsule) | {{< accel nvidia >}} | Planned | ✓ | [crossplane-provider-scaleway](https://github.com/scaleway/crossplane-provider-scaleway) (official) |
| Voltage Park (Managed Kubernetes) | {{< accel nvidia >}} | Planned | ✓ | none yet |
| Vultr (VKE) | {{< accel nvidia >}} {{< accel amd >}} | Planned | ✓ | [crossplane-provider-vultr](https://github.com/vultr/crossplane-provider-vultr) (official) |
{{< /table >}}
<!-- vale Modelplane.Spelling = YES -->
<!-- vale Vale.Terms = YES -->

Native provisioning expands as more Crossplane providers ship, and the
bring-your-own path means you can run on any conformant Kubernetes cluster, on a
neocloud or on-premise, right now.

{{< hint "tip" >}}
Don't see your cloud or neocloud, or want to be added?
[Open an issue](https://github.com/modelplaneai/modelplane/issues/new) and we'll
track it.
{{< /hint >}}

{{< cardgroup cols="2" >}}
{{< card title="Register a Cluster" href="/platform/inference-cluster/" >}}
Add a cluster to Modelplane, provisioned or bring-your-own.
{{< /card >}}
{{< card title="Define Hardware Classes" href="/platform/inference-class/" >}}
Describe the GPUs and provisioning recipe each node pool uses.
{{< /card >}}
{{< /cardgroup >}}
