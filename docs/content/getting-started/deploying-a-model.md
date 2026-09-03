---
title: Deploying a model
weight: 30
description: Declare what your model needs and serve it behind a unified endpoint.
---

Now that the platform is provisioned, the ML team can declare what a model needs
with a `ModelDeployment`. Describe the hardware requirements and the scheduler
schedules against the capacity the platform team published.

## Create a deployment

Create a namespace for the model:

```bash
kubectl create namespace ml-team
```

The device selector matches against the capacity declared in the
`InferenceClass`, not the pod's resource requests. Any L4 node satisfies
`>= 20Gi`, so this deployment runs on the cluster you just added:

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "getting-started/eks/model-deployment.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "getting-started/gke/model-deployment.yaml" >}}
{{< /tab >}}
{{< tab "AKS" >}}
{{< manifests "getting-started/aks/model-deployment.yaml" >}}
{{< /tab >}}
{{< tab "Nebius" >}}
{{< manifests "getting-started/nebius/model-deployment.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

Wait until `REPLICAS` shows `1`:

```bash
kubectl get md -n ml-team --watch
```

To see which cluster the scheduler chose:

```bash
kubectl get modelreplica -n ml-team
```

```shell{nocopy=true}
NAME              CLUSTER       SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   eks-us-east   True     True    modelreplicas.modelplane.ai   12m
```

The ML team never named a cluster. The scheduler matched the GPU requirement
(`>= 20Gi`) against the `InferenceClass` the platform team published and made
the placement. 

## Expose the model

A `ModelService` selects `ModelEndpoints` by label and publishes them as one
model a caller can name. Modelplane creates one `ModelEndpoint` per replica,
labeled with the deployment name:

{{< manifests "getting-started/model-service.yaml" >}}

Callers name the model rather than a path: it's `<namespace>/<service>`, so
`ml-team/qwen` here. The gateway rewrites that to whatever the engine was
started as, so the Hugging Face id this deployment serves under never reaches
the caller.

## Send a request

Read the OpenAI base URL from the gateway:

```bash
ADDRESS=$(kubectl get ig local -o jsonpath='{.status.endpoints.openAI}')
```

Send a request to it:

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -v "$ADDRESS/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"ml-team/qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}"'
```

The request routes to the replica on the cluster Modelplane placed it on.
You should get a response in a few seconds:

```json {nocopy=true}
{
  "id": "chatcmpl-c88b1429-067d-40a5-971c-ab9c54153c26",
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Kubernetes (K8s) is an open-source platform for automating 
        the deployment, scaling, and management of containerized applications. 
        It provides scalable orchestration capabilities that enable developers 
        to deploy complex applications quickly and efficiently across various environments."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 37,
    "completion_tokens": 48,
    "total_tokens": 85
  }
}

```

## Next step

The platform team declared capacity and in this guide the ML team deployed a
model behind a stable endpoint. Neither team needed to know what the other was doing. Modelplane matched them.

In the next step, the platform team grows the fleet. [Scale the platform]({{< ref "getting-started/scale-the-platform.md" >}}) to add more clusters across regions.

