---
title: Llama-3.1-8B
weight: 40
description: An 8B dense chat model on a single NVIDIA L4.
model: NousResearch/Meta-Llama-3.1-8B-Instruct
vendors: [Meta]
clouds: [EKS, GKE]
accelerators: [L4]
engines: [vLLM]
arch: Dense
precisions: ["BF16"]
size: 8B
ctx: "8,192"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.7.3]
gpuNote: 1× per node
---
<!-- vale write-good.Passive = NO -->
An 8B dense chat model on a single NVIDIA L4. The entry recipe: one `Standalone`
engine, no cache, public weights from a Hugging Face mirror. It carries no
`clusterSelector`, so device capacity alone matches it to any compatible L4 in
the fleet.

This recipe was run end to end on GKE; the `InferenceClass`, `InferenceCluster`,
and `ModelDeployment` are the exact manifests from that run. The EKS platform
shape is the standard single-L4 recipe. It passes server validation but was not
served in this run. Apply the platform side first, then the ML side. The GKE
`InferenceCluster` carries a GCP project placeholder to edit before applying.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "recipes/llama-3.1-8b/inference-class-eks.yaml" >}}

{{< manifests "recipes/llama-3.1-8b/inference-cluster-eks.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "recipes/llama-3.1-8b/inference-class-gke.yaml" >}}

{{< manifests path="recipes/llama-3.1-8b/inference-cluster-gke.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "recipes/llama-3.1-8b/inference-cluster-gke.yaml" >}} \
  | sed 's/my-gcp-project/$@<your-gcp-project-id>$@/' \
  | kubectl apply -f -
```
{{< /editCode >}}
{{< /tab >}}
{{< /tabs >}}

## Deployment

{{< manifests "recipes/llama-3.1-8b/model-deployment.yaml" >}}

{{< manifests "recipes/llama-3.1-8b/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
