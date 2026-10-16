# User-authored routing

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#502](https://github.com/modelplaneai/modelplane/issues/502), [#108](https://github.com/modelplaneai/modelplane/issues/108), [#179](https://github.com/modelplaneai/modelplane/issues/179)

## Summary

A `ModelDeployment` lets the user configure the engines but not the router.
Modelplane configures the router for one case: vLLM with its `NixlConnector`.
An SGLang prefill/decode deployment gets a router it can't use, and reports
Ready while it rejects requests (#502). Each new KV transfer format needs a
Modelplane release.

I propose the user writes the router for prefill/decode serving:

- `serving.router: EndpointPicker` hands the routing to the user. They set the
  endpoint picker's image and configuration in `serving.endpointPicker`, and
  write the sidecar into the decode pod as a container beside `engine` (#108).
- A `PrefillDecode` deployment must set both. Only `Unified` serving keeps a
  default, where the cluster routes the deployment.

```yaml
serving:
  mode: PrefillDecode
  router: EndpointPicker      # required under PrefillDecode
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
    config: { ... }           # required under PrefillDecode
```

## Background

On today's clusters, Modelplane's router is two components from
[llm-d](https://github.com/llm-d/llm-d-router):

- **The endpoint picker** chooses a pod for each request.
- **The sidecar** runs in each decode pod. It sends the prefill half of a
  request to the prefill pod that the picker chose.

The [base design](./design.md) gives the engines to the user and the router to
Modelplane. It says Modelplane shouldn't read or inject engine flags, and that
the user configures KV transfer in those flags. The base design keeps the picker
out of the API until a deployment needs to tune it. SGLang prefill/decode needs
to tune it.

### What must match

Prefill reads the prompt and makes the KV cache. Decode uses that cache to
generate tokens. The engines move the cache between pods, and the router tells
them which request it belongs to.

The router must match the engines in two settings. The sidecar's
[`--kv-connector`](https://github.com/llm-d/llm-d-router/blob/v0.9.0/docs/disaggregation.md)
must match the engines' KV connector. The picker's decider chooses whether a
request goes through prefill, and it must not skip prefill for an engine that
can't decode alone.

| Engine and KV connector | Sidecar `--kv-connector` | Decider can skip prefill |
|---|---|---|
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.31.0/features/nixl_connector_usage.html) | `nixlv2` | Yes |
| vLLM [`MooncakeConnector`](https://docs.vllm.ai/en/v0.31.0/features/mooncake_connector_usage.html) | `mooncake` | Yes |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `sglang` | No. Decode [rejects the request](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) with HTTP 400. |

### What Modelplane composes today

Modelplane configures the router for vLLM with NIXL:

- It injects the sidecar with `--kv-connector=nixlv2`.
- It configures a decider that skips prefill for short or cached prompts.
- It injects `VLLM_NIXL_SIDE_CHANNEL_HOST` and `VLLM_NIXL_SIDE_CHANNEL_PORT`
  into every engine pod.
- It reads `--port` from the decode engine's args, to find the engine behind the
  sidecar.
- It reads `--block-size` or `--page-size` from the engine's args, to configure
  prefix-cache scoring.

The user can't change the picker's configuration or image (#179).

This works only for vLLM with NIXL. SGLang needs a different decider, connector,
and pod label, and each new connector needs a Modelplane release. Router
releases break things too: llm-d-router v0.11.0 deprecates the sidecar's
`--vllm-port` flag and removes a plugin Modelplane uses.

### A default for prefill/decode

A wrong default costs `Unified` serving some load-balancing quality. It costs
`PrefillDecode` serving a pair that reports Ready and rejects requests. A
default that suits both vLLM and SGLang, such as an always-disaggregate decider,
would still have Modelplane assert which engines its configuration works for,
and the next engine that doesn't fit would fail the same way.

### Routers differ by serving stack

The endpoint picker protocol, from the Gateway API Inference Extension, is the
same for every picker: the gateway sends each request to the picker, and the
picker returns the pod. Everything else differs between projects:

| | llm-d | Dynamo 1.5 |
|---|---|---|
| Picker configuration | An `EndpointPickerConfig` | `DYN_*` environment variables for its native picker. A deprecated Go picker takes an `EndpointPickerConfig`. |
| Prefill/decode | The llm-d sidecar in the decode pod | A Dynamo frontend in the worker pods, or a standalone frontend with no picker |
| Engine | A stock engine, such as `vllm serve` | Dynamo's wrapper, such as `python -m dynamo.vllm` |

A prefill/decode deployment's engines are already specific to one stack. A
default router wouldn't make it portable.

## Goals

**Serve any engine the router supports.** A user can serve a new engine or KV
connector with no Modelplane release.

**Make prefill/decode routing explicit.** A `PrefillDecode` deployment states
its router and the router's configuration, so Modelplane never guesses which
engines it serves.

**Keep `Unified` simple.** A `Unified` deployment gets its cluster's routing
without writing a router. At most it labels its engine, because the cluster's
picker reads each engine's own metric names. An SGLang deployment sets
`llm-d.ai/engine-type: sglang`.

This design doesn't cover:

- Detecting a `PrefillDecode` pair that reports Ready but doesn't
  disaggregate, from a wrong decider, connector, or engine-type label, or a
  missing sidecar (#TBD). Modelplane can't check these from the API. llm-d's
  picker counts its decisions by type, such as `decode-only`, in
  `llm_d_epp_disagg_decision_total`, which a check could read.
- Routing to data-parallel ranks inside a pod (#500).

## Proposal

The API changes in three places:

1. `serving.router` names the router the user writes. Its one value is
   `EndpointPicker`. It's required under `PrefillDecode` and optional under
   `Unified`.
2. `serving.endpointPicker` sets the picker's image and configuration, which
   are set together. It's valid only with `router: EndpointPicker`, and
   required under `PrefillDecode`.
3. A member's template can hold containers beside `engine`, and a container can
   declare `ports`.

A `PrefillDecode` deployment writes its routing in the `ModelDeployment`. This
is the decode member of a vLLM deployment with NIXL. The comments mark each part
the user writes:

```yaml
serving:
  mode: PrefillDecode
  router: EndpointPicker
  endpointPicker:
    image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0  # the picker image
    config: { ... }                  # the picker configuration
engines:
- name: decode
  phase: Decode
  members:
  - role: Standalone
    template:
      spec:
        containers:
        - name: sidecar              # the sidecar, on port 8000
          image: ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0
          args:
          - --secure-proxy=false
          - --kv-connector=nixlv2    # matches the engine's connector
          - --vllm-port=8001
          ports:
          - containerPort: 8000
        - name: engine
          image: vllm/vllm-openai:v0.23.0
          args:
          - --port=8001
          - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}
          env:                       # the KV transfer environment
          - name: VLLM_NIXL_SIDE_CHANNEL_HOST
            valueFrom:
              fieldRef:
                fieldPath: status.podIP
          - name: VLLM_NIXL_SIDE_CHANNEL_PORT
            value: "5557"
          ports:
          - name: http               # the engine's port
            containerPort: 8001
```

The prefill member sets the same environment, without the sidecar. Modelplane
injects neither the sidecar nor the environment. A multi-node decode engine
puts the sidecar in its `Leader` member's template.

Modelplane composes the rest:

- The InferencePool and the HTTPRoute.
- The picker's Deployment, Service, and RBAC, and the flags that connect the
  picker to the InferencePool.
- The `llm-d.ai/role` label on each `PrefillDecode` engine's pods, from its
  `phase`.
- A memory-backed `/dev/shm` on each engine container.

The Kimi-K2 recipe changes to this form. This is a breaking change to
`v1alpha1`. After the upgrade, a `PrefillDecode` replica without its routing
stops reconciling: `compose-model-replica` returns an error, and leaves the
running pods and router in place until the `ModelDeployment` sets `router` and
`endpointPicker`. The deployment must add the sidecar too, though Modelplane
can't check for it.

### Unified serving

A `Unified` deployment that doesn't set `router` gets its cluster's routing. On
every cluster today, that's llm-d's picker with Modelplane's default
configuration, which reads the block size from `--block-size` or `--page-size`,
or uses vLLM's default of 16. A `Unified` deployment can set `router:
EndpointPicker` to tune the picker.

### Placement

`router: EndpointPicker` runs only on a cluster that composes an InferencePool
for a picker the user names. Each `InferenceCluster` lists the routers it can
compose in `status.routers`, which `compose-inference-cluster` sets from the
cluster's serving stack. Every cluster today lists `EndpointPicker`, on either
stack. With the stack-aware placement in #418, the scheduler places a deployment
that sets `router` only on those clusters, and reports `ReplicasScheduled=False`
when none qualify.

### The endpoint picker

`serving.endpointPicker` is valid only with `router: EndpointPicker`:

- `image` is the picker image, and `config` is its configuration. Modelplane
  passes the configuration to the picker and doesn't validate it.
- `image` and `config` are set together. The configuration format changes
  between router releases. A configuration states the picker it's for, and a
  pinned picker never runs Modelplane's default configuration.
- A `config` replaces the default whole. Modelplane doesn't merge them.
- A `Unified` deployment that omits both gets Modelplane's picker and default
  configuration.

Modelplane runs the image with the command line of llm-d's router, which the
Gateway API Inference Extension's reference picker shares:

```text
--pool-name=<replica>-pool --pool-namespace=<namespace>
--pool-group=inference.networking.k8s.io --config-file=/config/epp-config.yaml
--grpc-port=9002 --metrics-port=9090 --metrics-endpoint-auth=false
```

The picker serves gRPC on 9002. The InferencePool selects the replica's serving
pods by `modelplane.ai/serving`, and sends to port 8000. Under `PrefillDecode`,
Modelplane labels each engine's pods `llm-d.ai/role: prefill` or `llm-d.ai/role:
decode`, which llm-d's `prefill-filter` and `decode-filter` select on. In either
mode it labels each serving pod `app` and `llm-d.ai/inference-serving`,
replacing any `app` label the template sets. If the picker rejects its
configuration, a new replica isn't Ready. A running replica keeps its old
picker, which serves the old configuration, and stays Ready. Envoy AI
Gateway fails closed without a picker, whatever the InferencePool's failure
mode. A picker outage stops requests rather than sending them to prefill pods.

### Containers beside the engine

This design adopts #108, in either serving mode. A member's template can list
containers beside `engine`:

- Exactly one container is named `engine`, and container names are unique.
- A container beside `engine` takes the same fields as `engine`: `image`,
  `command`, `args`, `env`, and `ports`.
- Modelplane doesn't change any container except `engine`. It adds device
  claims, `MODELPLANE_` environment, ModelCache mounts, `/dev/shm`, and the
  readiness probe. The pod's readiness follows the engine.
- Each member has its own template. The user chooses which pods of a gang get a
  container.

### Ports

A serving pod receives requests on port 8000, as today. The engine's port named
`http` is where Modelplane probes the engine and the collector scrapes its
metrics. It defaults to 8000, so an engine alone in its pod declares nothing.

When a container is in front of the engine, such as the sidecar:

- That container declares port 8000, and passes requests to the engine. It must
  pass `/metrics` through too, because the picker reads it there.
- The engine declares its `http` port on another port, such as 8001. The probe
  must reach the engine itself, because the llm-d sidecar answers `/health`
  without asking the engine.

A container that doesn't serve, such as a log shipper, doesn't declare a port,
and the engine doesn't either. CEL allows at most one container to declare 8000,
and requires one when the engine's `http` port is elsewhere. Only serving pods
(a `Standalone` pod, or a gang's leader) get the probe, so a `Worker` member
doesn't need a container on 8000.

### Example

This SGLang deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each
phase. The engines come from the [Laguna SGLang
recipe](../docs/manifests/recipes/laguna/model-deployment-sglang.yaml), with the
disaggregation flags from llm-d's [SGLang
guide](https://github.com/llm-d/llm-d/tree/v0.9.0/guides/pd-disaggregation/modelserver/gpu/sglang/base).

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: laguna-pd
  namespace: ml-team
spec:
  replicas: 1
  template:
    spec:
      modelCacheRef:
        name: laguna
      serving:
        mode: PrefillDecode
        router: EndpointPicker
        endpointPicker:
          image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
          config:
            apiVersion: llm-d.ai/v1alpha1
            kind: EndpointPickerConfig
            plugins:
            - type: approx-prefix-cache-producer
              parameters:
                autoTune: false
                blockSizeTokens: 64
            - type: prefix-cache-scorer
            - type: queue-scorer
            - type: prefill-filter
            - type: decode-filter
            - type: max-score-picker
            - type: always-disagg-pd-decider
            - type: disagg-profile-handler
              parameters:
                deciders:
                  prefill: always-disagg-pd-decider
            schedulingProfiles:
            - name: prefill
              plugins:
              - pluginRef: prefill-filter
              - pluginRef: max-score-picker
              - pluginRef: prefix-cache-scorer
                weight: 2
              - pluginRef: queue-scorer
                weight: 1
            - name: decode
              plugins:
              - pluginRef: decode-filter
              - pluginRef: max-score-picker
              - pluginRef: prefix-cache-scorer
                weight: 2
              - pluginRef: queue-scorer
                weight: 1
      engines:
      - name: prefill
        phase: Prefill
        members:
        - role: Standalone
          nodeSelector:
            devices:
            - name: gpu
              count: 8
              selectors:
              - cel: |
                  device.driver == "gpu.nvidia.com" && device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("79Gi")) >= 0
          template:
            metadata:
              labels:
                llm-d.ai/engine-type: sglang
            spec:
              containers:
              - name: engine
                image: lmsysorg/sglang:v0.5.12.post1-cu129
                command: ["python3", "-m", "sglang.launch_server"]
                args:
                - --model-path=poolside/Laguna-S-2.1-FP8
                - --served-model-name=$(MODELPLANE_SERVED_MODEL_NAME)
                - --tp-size=8
                - --context-length=262144
                - --trust-remote-code
                - --tool-call-parser=poolside_v1
                - --reasoning-parser=poolside_v1
                - --host=0.0.0.0
                - --port=8000
                - --disaggregation-mode=prefill
                - --disaggregation-transfer-backend=nixl
                - --enable-metrics
      - name: decode
        phase: Decode
        members:
        - role: Standalone
          nodeSelector:
            devices:
            - name: gpu
              count: 8
              selectors:
              - cel: |
                  device.driver == "gpu.nvidia.com" && device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("79Gi")) >= 0
          template:
            metadata:
              labels:
                llm-d.ai/engine-type: sglang
            spec:
              containers:
              - name: sidecar
                image: ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0
                args:
                - --secure-proxy=false
                - --kv-connector=sglang
                - --vllm-port=8001
                ports:
                - containerPort: 8000
              - name: engine
                image: lmsysorg/sglang:v0.5.12.post1-cu129
                command: ["python3", "-m", "sglang.launch_server"]
                args:
                - --model-path=poolside/Laguna-S-2.1-FP8
                - --served-model-name=$(MODELPLANE_SERVED_MODEL_NAME)
                - --tp-size=8
                - --context-length=262144
                - --trust-remote-code
                - --tool-call-parser=poolside_v1
                - --reasoning-parser=poolside_v1
                - --host=0.0.0.0
                - --port=8001
                - --disaggregation-mode=decode
                - --disaggregation-transfer-backend=nixl
                - --enable-metrics
                ports:
                - name: http
                  containerPort: 8001
```

These router settings must match the engines:

| Router setting | Why |
|---|---|
| `--kv-connector=sglang` on the sidecar | The engines are SGLang. |
| `always-disagg-pd-decider` in the picker configuration | An SGLang decode engine rejects a request that skips prefill. |
| `llm-d.ai/engine-type: sglang` on the pods | The picker reads SGLang's metric names. Without it, the picker sees a queue depth of zero. |
| `blockSizeTokens: 64` in the picker configuration | llm-d's SGLang guidance: 64, or the engine's page size if it's larger. |

## Future improvements

### Dynamo

A cluster that composes a DynamoGraphDeployment would route `Unified`
deployments with Dynamo by default. Dynamo 1.5's own picker takes no flags and
reads `DYN_*` environment variables, so prefill/decode on Dynamo needs its own
`router` value rather than `EndpointPicker`. A `Unified` deployment keeps the
unchanged API that [serving stacks](./serving-stacks.md) promises Dynamo
users. A `PrefillDecode` deployment names its router on any stack.

### More container fields

A namespace whose LimitRange or quota requires resource limits needs
`resources` on every container. Containers beside the engine can't set them
yet. The API can add `resources` to all containers at once. Containers can't
mount volumes either. The sidecar runs with `--secure-proxy=false`, because
`--cert-path` has nothing to point at.

Modelplane probes only the engine, so a decode pod is Ready once its
engine is, whether or not the sidecar listens. The sidecar starts in about a
second and the engine in minutes, so this rarely matters. A `readinessProbe` on
every container would close it.

### Shared picker configurations

Deployments that use the same engine repeat the same picker configuration. If
recipes show this, a cluster-scoped resource can hold a named configuration that
`endpointPicker` refers to. MetricMapping is a similar resource today.

## Alternatives considered

### A sidecar field

`serving.prefillDecode.sidecar` would be a second way to write a container, for
routing only. llm-d is also replacing the sidecar with a
[coordinator](https://github.com/llm-d/llm-d-router/blob/main/docs/coordinator_architecture.md),
which would leave the field with nothing to configure.
