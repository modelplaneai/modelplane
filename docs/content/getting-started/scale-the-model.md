---
title: Scale the model
weight: 50
description: Serve the model from two regions behind a single endpoint.
---
A `ModelService` can front more than one `ModelDeployment`. Here you add a second
deployment, pinned to a different region, and point the same service at both. The
endpoint you already curled stays the same. Behind it, traffic now load-balances
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

The endpoint URL doesn't change. Clients that had this URL before still have it;
they don't know the fleet changed. The gateway load-balances across both regions,
and losing one region keeps the other serving. Send the same request as before:

```bash
ADDRESS=$(kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}')
```

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -v "$ADDRESS/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}"'
```

## That's the tour

You stood up a control plane, built a multi-region GPU fleet, deployed a model
across it, and ended with one stable endpoint serving requests. The platform
team published hardware. The ML team described what the model needs. Modelplane
placed them and served behind a single endpoint.

[Clean up]({{< ref "getting-started/clean-up.md" >}}) tears everything down
when you're done.

For more on the resources you used:

* [InferenceClass]({{< ref "platform/inference-class.md" >}})
* [InferenceCluster]({{< ref "platform/inference-cluster.md" >}})
* [ModelDeployment]({{< ref "models/model-deployment.md" >}})
* [ModelService]({{< ref "models/model-service.md" >}})

Modelplane is in active development and we're building in the open. If you're
running your own inference fleet and want to shape where this goes, we'd love to
hear from you. Star the [repository](https://github.com/modelplaneai/modelplane),
join us in [Slack](https://slack.modelplane.ai), or read the
[manifesto](https://modelplane.ai).
