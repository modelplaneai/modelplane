---
title: Try Modelplane locally
weight: 20
description: Deploy a model and fail it over on your laptop, with no cloud account and no GPU.
---

Modelplane splits AI inference between two teams. The platform or inference team
publishes the GPU capacity in its fleet, and the ML team declares what each
model needs.
Modelplane matches the two, so neither team waits on the other. The platform
team can add or replace clusters without touching a deployment, and the ML team
can change models without filing a request.

This demo walks you through the Modelplane workflow on two kind clusters on your
machine. A setup script creates the clusters. Then you build the inference
platform like a platform team, and upgrade a model behind the same name like an
ML team.

## Prerequisites

Before you begin, make sure you have:

- Docker, with at least 10 GB of memory on Docker Desktop. Both clusters and
  the model run on your machine.
- [kind](https://kind.sigs.k8s.io/) installed
- kubectl installed
- Helm installed
- [`jq`](https://jqlang.org/) installed

## Set up the clusters

Run the setup script. It takes about 10 minutes:

```bash
curl -fsSL {{< manifest-url "getting-started/local-demo/setup.sh" >}} | bash
```

The script creates two kind clusters:

- `modelplane`, the control plane. It runs Crossplane and Modelplane, and
  holds every Modelplane object in this demo.
- `modelplane-workload`, the workload cluster. It replaces the GPU cluster
  you'd run in a cloud account. MetalLB gives it load balancer addresses, and
  [`dra-example-driver`](https://github.com/kubernetes-sigs/dra-example-driver)
  gives it eight fake GPUs.

It also stores the workload cluster's kubeconfig in a `Secret` on the control
plane. When the script finishes, `kubectl` points at the control plane.

## Build the platform

The platform team describes its fleet to Modelplane. It publishes hardware
profiles and registers the clusters that offer them. It also runs the gateway
that callers send requests to. The platform team writes these objects without
needing to know which models will run.

Apply the platform manifest:

{{< manifests "getting-started/local-demo/platform.yaml" >}}

The `InferenceClass` publishes a hardware profile: one GPU with 80Gi of
memory. ML teams match their models against classes, so they can choose
hardware without knowing which clusters exist.

The `InferenceCluster` registers the workload cluster through the kubeconfig
the script stored. It offers a node pool with `synthetic-gpu` hardware.
Modelplane installs its serving stack on the cluster, including Envoy Gateway
and llm-d. This demo registers one cluster, but Modelplane is built to manage a
fleet of them, and the platform team can add more without changing any model.

The `InferenceGateway` gives callers one address, however many clusters serve
the models behind it. It accepts OpenAI API requests and routes each one to a
cluster serving the model it names.

The `ml-team` namespace holds the ML team's objects, mainly the models you
deploy in the next section.

Wait for Modelplane to install the serving stack and bring up the gateway:

```bash
kubectl wait ic/local --for=condition=Ready --timeout=15m
kubectl wait ig/public --for=condition=Ready --timeout=10m
```

The gateway's address is on kind's Docker network, which your machine can't
reach on macOS or Windows. Forward `localhost:8080` to it:

```bash
curl -fsSL {{< manifest-url "getting-started/local-demo/setup.sh" >}} | bash -s -- --expose
```

## Deploy a model

A `ModelDeployment` describes a model and the hardware it needs. A
`ModelService` gives one or more deployments a name that callers use, in the
form `<namespace>/<service>`. Applications call the name, and the team can
change the models behind it.

The ML team wants an assistant that answers technical questions. Start with
SmolLM-135M-Instruct, the first generation of Hugging Face's SmolLM models, and
name it `ml-team/assistant`:

{{< manifests "getting-started/local-demo/smollm.yaml" >}}

Wait for the deployment to report `Ready`, and for the gateway to route to it:

```bash
kubectl -n ml-team wait md/smollm --for=condition=Ready --timeout=10m
kubectl -n ml-team wait modelservice/assistant --for=condition=RoutingReady --timeout=10m
```

The `nodeSelector` asks for one GPU with at least 20Gi of memory, and names no
cluster. It's a Kubernetes
[Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/resource-management/dynamic-resource-allocation/)
(DRA) request, the same one you'd write for a pod. Modelplane compares it with
the classes the platform team published, finds that `synthetic-gpu` fits, and
places a replica on a cluster that offers it. See where the replica went:

```bash
kubectl -n ml-team get modelreplica
```

The `CLUSTER` column shows `local`. On a real fleet, the platform team can add,
replace or move clusters, and the deployment stays the same.

llama.cpp runs the model on the workload cluster's CPU. The fake GPU is a DRA
device that gives the scheduler something to claim.

## Call the model

The gateway answers on `localhost:8080`. Send it a request in
the OpenAI format:

```bash
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "ml-team/assistant", "messages": [{"role": "user", "content": "What is Kubernetes?"}]}' \
  | jq -r '.model, .choices[0].message.content'
```

The `model` field in the response shows which deployment answered,
`ml-team/smollm`. Your request asked for one model name, `ml-team/assistant`. From that
name, the gateway found the `smollm` endpoint and forwarded the request.
Callers don't hold a cluster, address or engine name, which leaves the team
free to replace the model.

## Swap in a better model

Read the answer. For better ones, the team turns to SmolLM2-360M-Instruct.
Hugging Face trained SmolLM2 on more data, and this model has close to three
times the parameters.

A `ModelService` can list several endpoints, each with a priority. Requests go
to the priority 0 endpoint while it has a ready replica, and to the priority 1
endpoint when it has none.

Deploy SmolLM2-360M-Instruct, put it in `assistant` at priority 0, and move
the first model to priority 1:

{{< manifests "getting-started/local-demo/smollm2.yaml" >}}

Wait for the deployment to report `Ready`:

```bash
kubectl -n ml-team wait md/smollm2 --for=condition=Ready --timeout=10m
```

Run the request again to show the switch:

```bash
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "ml-team/assistant", "messages": [{"role": "user", "content": "What is Kubernetes?"}]}' \
  | jq -r '.model, .choices[0].message.content'
```

Now the `model` field shows `ml-team/smollm2`. Your request stayed the same,
and so did the name it calls. You changed the `ModelService`, and the gateway
picked up the new model.

## Fall back to the old model

The first model still sits in `assistant` at priority 1. If SmolLM2 loses its
replicas, the gateway sends requests to the first model. Scale SmolLM2 to zero
replicas:

```bash
kubectl -n ml-team scale md smollm2 --replicas=0
```

Run the request again:

```bash
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "ml-team/assistant", "messages": [{"role": "user", "content": "What is Kubernetes?"}]}' \
  | jq -r '.model, .choices[0].message.content'
```

The gateway takes a few seconds to move the route off SmolLM2. If the `model`
still shows `ml-team/smollm2`, wait a moment and run the request again.

The `model` field shows `ml-team/smollm` again. Requests kept reaching a model while
SmolLM2 had no replicas. Scale SmolLM2 back up:

```bash
kubectl -n ml-team scale md smollm2 --replicas=1
kubectl -n ml-team wait md/smollm2 --for=condition=Ready --timeout=10m
```

Watch it take the requests again:

```bash
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model": "ml-team/assistant", "messages": [{"role": "user", "content": "What is Kubernetes?"}]}' \
  | jq -r '.model, .choices[0].message.content'
```

To retire the first model, remove its endpoint from `assistant` and delete its
`ModelDeployment`. Callers keep using `ml-team/assistant`.

## Clean up

Delete both clusters:

```bash
curl -fsSL {{< manifest-url "getting-started/local-demo/setup.sh" >}} | bash -s -- --clean
```

## Next steps

[Take the tour]({{< ref "/install" >}}) to run Modelplane on a cloud account,
with GPU clusters across regions.
