---
title: Scale the model
weight: 50
description: Serve the model from two regions behind a single endpoint.
---
A `ModelService` can front more than one `ModelDeployment`. Here you add a second
deployment, pinned to a different region, and point the same service at both. The
endpoint you already curled stays the same; behind it, traffic now load-balances
across two regions.

```mermaid
graph LR
    subgraph fleet ["Fleet"]
        IC1["us-east\nL4"]
        IC2["us-west\nlarger GPU"]
    end

    subgraph ml ["ML team"]
        MD1["ModelDeployment\nqwen-demo"]
        MD2["ModelDeployment\nqwen-west\nclusterSelector: us-west"]
        MS["ModelService qwen\n/ml-team/qwen/v1/..."]
    end

    IC1 --> MD1
    IC2 --> MD2
    MD1 --> MS
    MD2 --> MS
```

## Deploy to a second region

The new deployment uses a `clusterSelector` to pin its replica to the `us-west`
cluster you added in the last step, and selects the larger GPU there:

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "getting-started/eks/model-deployment-west.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "getting-started/gke/model-deployment-west.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

Wait until its replica is `Ready`, then check placement. You now have one replica
per region:

```bash
kubectl get modelreplica -n ml-team
```

```shell {nocopy=true}
NAME              CLUSTER       SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   eks-us-east   True     True    modelreplicas.modelplane.ai   42m
qwen-west-92535   eks-us-west   True     True    modelreplicas.modelplane.ai   8m
```

## Front both with one service

Update the `ModelService` to select both deployments. Each entry in
`spec.endpoints` adds its matching replicas to the same endpoint:

{{< manifests "getting-started/model-service-multi.yaml" >}}

The endpoint URL doesn't change. The gateway now load-balances `/ml-team/qwen/`
across both regions, and losing one region keeps the other serving. Send the same
request as before to confirm it still answers:

```bash
ADDRESS=$(kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}')

kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  -- curl -s http://$ADDRESS/ml-team/qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "What is Crossplane in one sentence?"}],
    "max_tokens": 100
  }'
```

## That's the tour

You stood up a control plane, built a multi-region GPU fleet, and served one
model across it behind a single endpoint, separating the platform team's job
(publishing hardware) from the ML team's job (declaring what a model needs).

When you're done, [clean up]({{< ref "getting-started/clean-up.md" >}}) to tear
everything down.

For more on the resources you used:

* [InferenceClass]({{< ref "platform/inference-class.md" >}})
* [InferenceCluster]({{< ref "platform/inference-cluster.md" >}})
* [ModelDeployment]({{< ref "models/model-deployment.md" >}})
* [ModelService]({{< ref "models/model-service.md" >}})

Star the [Modelplane project on GitHub](https://github.com/modelplaneai/modelplane) and build with us.
