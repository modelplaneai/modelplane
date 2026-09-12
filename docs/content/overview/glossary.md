---
title: Glossary
weight: 37
description: Terms used throughout the Modelplane docs and what they mean.
---

## Modelplane

The open source control plane software. You install Modelplane on a Kubernetes
cluster (the **control cluster**). Modelplane never serves tokens itself; it
orchestrates the clusters and engines that do.

## Control cluster

The Kubernetes cluster where Modelplane runs. It needs no GPUs. It holds
Modelplane's Crossplane-based components and the API resources you apply to
declare your fleet.

## Inference cluster

A GPU cluster in the fleet where serving engines run and tokens are produced.
Modelplane can provision inference clusters on EKS, GKE, and other providers, or
you can bring your own through an `InferenceCluster` with `source: Existing`.

## Fleet

All inference clusters managed by a single Modelplane control cluster.

## Serving stack

The per-cluster software layer that turns a stock engine into a routable serving
instance: `Standard` (the default, Modelplane-composed Deployments and
LeaderWorkerSets) or `Dynamo` (NVIDIA's Grove, KAI Scheduler, and ModelExpress).
Set by `InferenceCluster.spec.stack`.

## Platform

The inference infrastructure the platform team
provisions using `InferenceGateway`, `InferenceClass`, and `InferenceCluster`
resources. This is distinct from Modelplane itself, which runs on the control
cluster above the fleet.

## Platform team

The infrastructure team responsible for GPU capacity. They create
`InferenceCluster`, `InferenceClass`, and `InferenceGateway` resources,
provisioning the fleet that ML teams deploy against.

<!-- vale Google.Headings = NO -->
<!-- vale Microsoft.HeadingAcronyms = NO -->
## ML team
<!-- vale Google.Headings = YES -->
<!-- vale Microsoft.HeadingAcronyms = YES -->

The development team deploying models. They create `ModelDeployment`,
`ModelService`, and `ModelCache` resources, declaring what a model needs without
knowing which cluster it runs on.
