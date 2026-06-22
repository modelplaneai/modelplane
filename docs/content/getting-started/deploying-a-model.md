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

A `ModelService` selects `ModelEndpoints` by label and creates a Gateway API
`HTTPRoute` that routes to them. Modelplane creates one `ModelEndpoint` per
replica, labeled with the deployment name:

{{< manifests "getting-started/model-service.yaml" >}}

The request path is `/<namespace>/<modelservice-name>/...` (`/ml-team/qwen/` in
this example), from the `ModelService` named `qwen`. The `model` field in the
request body is the Hugging Face id `Qwen/Qwen2.5-0.5B-Instruct`, since this
deployment doesn't set `--served-model-name`.

## Send a request

Read the endpoint's public address from the `ModelService` status:

```bash
ADDRESS=$(kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}')
```

Send a request to it:

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -v "$ADDRESS/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"What is the squre root of pi?\"}],\"max_tokens\":100}"'
```

The request routes to the replica on the cluster Modelplane placed it on.
You should get a response in a few seconds:

```json {nocopy=true}
{
  "id": "chatcmpl-a26fc9ab-a7e6-4000-a349-286227102d57",
  "object": "chat.completion",
  "created": 1782139786,
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The square root of pi (π) is approximately 
        1.77245385090551602779409473109902323386.
        This value can be obtained through various mathematical methods or 
        algorithms designed to compute the square root of π. The precision and 
        accuracy of this value depend on the computational resources available 
        and the method used for calculation.",
      },
      "finish_reason": "stop",
    }
  ],
  "usage": {
    "prompt_tokens": 37,
    "completion_tokens": 93,
    "total_tokens": 130,
  },
}
```

## Next step

The platform team declared capacity and in this guide the ML team deployed a
model behind a stable endpoint. Neither team needed to know what the other was doing. Modelplane matched them.

In the next step, the platform team grows the fleet. [Scale the platform]({{< ref "getting-started/scale-the-platform.md" >}}) to add more clusters across regions.

