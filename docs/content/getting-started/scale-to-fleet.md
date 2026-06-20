---
title: Scale to a multi-cluster fleet
weight: 20
description: Extend a single cluster to a fleet and schedule models by hardware capability.
---

<!-- vale write-good.Passive = NO -->
This guide extends the setup from [the previous guide]({{< ref
"getting-started/first-deployment.md" >}}). 

You'll add two larger GPU clusters in different regions and raise the memory
threshold in the `qwen-demo` deployment. Modelplane moves the replicas to the
qualifying hardware.

By the end, one `ModelDeployment` will run replicas across two larger-GPU clusters,
routed through the same endpoint you curled in Part 1. The `L4` cluster will
still be present but skipped because it no longer meets the selector.

Provisioning two more clusters takes about 10–15 minutes.


## Scale your inference fleet

<!-- vale write-good.TooWordy = NO -->

{{< tabs >}}
{{< tab "EKS" >}}
Register two more clusters with a bigger hardware class: `L40S` (`48 Gi`):

{{< manifests "getting-started/eks/platform-scale.yaml" >}}

{{< hint "note" >}}
`g6e.xlarge` runs ~$2/hr on demand. Two of them plus the `L4` from Part 1 is a
few dollars for this guide. Delete the clusters when you're done (see [Clean
up](#clean-up)).
{{< /hint >}}
{{< /tab >}}
{{< tab "GKE" >}}
Register two more clusters with a bigger hardware class: `A100` (`40 Gi`).
Apply the manifest, setting each cluster's `project` to your GCP project:

{{< manifests path="getting-started/gke/platform-scale.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "getting-started/gke/platform-scale.yaml" >}} \
  | sed 's/my-gcp-project/$@<your-gcp-project>$@/g' \
  | kubectl apply -f -
```
{{< /editCode >}}

{{< hint "note" >}}
`a2-highgpu-1g` runs ~$3.50/hr on demand. Two of them plus the `L4` from Part 1 is a
few dollars for this guide. Delete the clusters when you're done (see [Clean
up](#clean-up)).
{{< /hint >}}
{{< /tab >}}
{{< /tabs >}}

<!-- vale write-good.TooWordy  = YES -->

Modelplane provisions both clusters in parallel:

```bash
kubectl wait --for=condition=Ready ic --all --timeout=20m
```

## Request new hardware for your model

```mermaid
graph LR
    subgraph pt ["Platform team"]
        IC1["starter cluster\nL4 · 24Gi"]
        IC2["scale cluster 1\nlarger GPU · 40+ Gi"]
        IC3["scale cluster 2\nlarger GPU · 40+ Gi"]
    end

    sel["memory > L4 threshold"]

    IC2 -- "40+ Gi ✓" --> sel
    IC3 -- "40+ Gi ✓" --> sel
    IC1 -. "24Gi · skipped" .-> sel

    subgraph ml ["ML team"]
        MD["ModelDeployment\nhigher memory threshold\nreplicas: 2"]
        EP["unified endpoint\n/ml-team/qwen/v1/..."]
    end

    sel --> MD
    MD --> EP
```
Update the `qwen-demo` deployment with a higher memory threshold and two replicas:

{{< tabs >}}
{{< tab "EKS" >}}
{{< manifests "getting-started/eks/model-deployment-scale.yaml" >}}
{{< /tab >}}
{{< tab "GKE" >}}
{{< manifests "getting-started/gke/model-deployment-scale.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

Wait until `REPLICAS` shows `2`:

```bash
kubectl get md -n ml-team --watch
```

Check replica placement:

```bash
kubectl get modelreplica -n ml-team
```

{{< tabs >}}
{{< tab "EKS" >}}
```shell {nocopy=true}
NAME              CLUSTER        SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   eks-us-west      True     True    modelreplicas.modelplane.ai   8m
qwen-demo-92535   eks-eu-central   True     True    modelreplicas.modelplane.ai   29m
```
{{< /tab >}}
{{< tab "GKE" >}}
```shell {nocopy=true}
NAME              CLUSTER        SYNCED   READY   COMPOSITION                   AGE
qwen-demo-7323a   gpu-us-west   True     True    modelreplicas.modelplane.ai   8m
qwen-demo-92535   gpu-us-east   True     True    modelreplicas.modelplane.ai   29m
```
{{< /tab >}}
{{< /tabs >}}

The endpoint URL doesn't change. The gateway picks up the new replicas
automatically.

Any new qualifying cluster that becomes `Ready` is eligible automatically. The same
`ModelService` fronts both regions, so losing one cluster keeps the other
serving.

## Clean up

Delete model resources before clusters:

```bash
kubectl delete md --all -n ml-team
kubectl delete ms --all -n ml-team
```

Wait for all model replicas to finish:

```bash
kubectl get modelreplica -n ml-team --watch
```

Delete all clusters with foreground cascading deletion. The serving stack on
each workload cluster must uninstall while that cluster's API server is still
reachable. Foreground deletion holds each cluster object until its stack
finishes; background deletion can orphan cloud resources.

```bash
kubectl delete ic --all --cascade=foreground
```

Wait until all clusters are deleted:

```bash
kubectl get ic --watch
```

Delete the kind cluster:

```bash
kind delete cluster --name modelplane
```
<!-- vale write-good.Passive = YES -->

## Next steps

In this guide, you scaled an inference stack deployment hardware to support 
your model deployment. You created new clusters and were able to deploy models
to the appropriate cluster based on hardware needs.

* [ModelDeployment]({{< ref "models/model-deployment.md" >}})
* [InferenceCluster]({{< ref "platform/inference-cluster.md" >}})

Star the [Modelplane project on GitHub](https://github.com/modelplaneai/modelplane) and build with us.
