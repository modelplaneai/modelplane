---
title: Configure Routing
weight: 15
description: Write the endpoint picker and the prefill/decode sidecar for your engines.
---
<!-- vale write-good.Passive = NO -->
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})

Modelplane routes each request from the cluster edge to one of your engines.
This page uses these terms:

| Term | Meaning |
|---|---|
| Router | Everything between the cluster edge and your engines that picks a pod and, for prefill/decode, moves the request between phases |
| Endpoint picker | The component that picks a pod for each request, such as [llm-d's](https://github.com/llm-d/llm-d-router) |
| Decider | The part of the picker's configuration that chooses whether a request goes through prefill |
| The sidecar | A container in each decode pod that sends the prefill half of a request to the prefill pod the picker chose |
| KV connector | The engine setting that moves the KV cache between pods, such as vLLM's `NixlConnector` |

How much you write depends on the serving mode:

- **`Unified`.** Modelplane routes your deployment with llm-d's endpoint
  picker, which picks a pod by prefix cache and load. You don't write anything.
  To tune the picker, set `serving.router: EndpointPicker` and write the
  picker's container and configuration in `serving.endpointPicker`.
- **`PrefillDecode`.** You write the router. Set
  `serving.router: EndpointPicker`, the picker in `serving.endpointPicker`,
  and the sidecar in the decode member. Modelplane
  rejects a `PrefillDecode` deployment without `router` and `endpointPicker`.
  It warns when the decode pod has no container beside the engine, but it
  can't tell whether that container is the sidecar.

## What you write for prefill/decode

This decode member is vLLM with NIXL, and the comments mark what you write. The
[disaggregated example]({{< ref "model-deployment.md#examples" >}}) is the whole
deployment.

```yaml {nocopy=true}
serving:
  mode: PrefillDecode
  router: EndpointPicker
  endpointPicker:
    template: { ... }                # the picker container
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
          readinessProbe:            # ready once it listens
            httpGet:
              path: /health
              port: 8000
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

The prefill member sets the same environment. It has no sidecar, and its engine
serves on 8000, so it doesn't declare a port. For a multi-node decode engine,
the sidecar goes in the `Leader` member's template.

Modelplane composes the InferencePool, the HTTPRoute, and the picker's
Deployment, Service, and RBAC. It gives each engine container a `/dev/shm` in
memory. It doesn't add the sidecar or any KV transfer environment.

## The endpoint picker

`serving.endpointPicker.template` is the picker's pod, with one container named
`epp`. You write its image and command line, because each picker spells its
flags differently. Modelplane sets these variables in its environment, for the
args to pass on:

| Variable | Value |
|---|---|
| `MODELPLANE_POOL_NAME` | The InferencePool the picker serves |
| `MODELPLANE_POOL_NAMESPACE` | The pool's namespace |
| `MODELPLANE_PICKER_CONFIG` | The path of the mounted `config`, when you set one |
| `MODELPLANE_GRPC_PORT` | The port the gateway calls the picker on, 9002 |
| `MODELPLANE_GRPC_HEALTH_PORT` | The port of the picker's gRPC health service, 9003 |
| `MODELPLANE_METRICS_PORT` | The port the picker serves Prometheus metrics on, 9090 |

The picker must serve on the three ports. Modelplane declares them, and the
picker is ready when its gRPC health service says so. This container runs llm-d's
picker:

```yaml {nocopy=true}
containers:
- name: epp
  image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
  args:
  - --pool-name=$(MODELPLANE_POOL_NAME)
  - --pool-namespace=$(MODELPLANE_POOL_NAMESPACE)
  - --pool-group=inference.networking.k8s.io
  - --config-file=$(MODELPLANE_PICKER_CONFIG)
  - --grpc-port=$(MODELPLANE_GRPC_PORT)
  - --grpc-health-port=$(MODELPLANE_GRPC_HEALTH_PORT)
  - --metrics-port=$(MODELPLANE_METRICS_PORT)
  - --metrics-endpoint-auth=false
```

llm-d's picker authenticates metrics scrapes by default, which rejects the
collector's, so keep `--metrics-endpoint-auth=false`. The container can also
set `env` and `resources`, and the template can set `imagePullSecrets`.

`config` is the picker's configuration, such as an llm-d
`EndpointPickerConfig`. Modelplane mounts it without checking it, and it
replaces the default configuration rather than merging with it. Leave it out
for a picker that takes no configuration.

Under `Unified`, leave out `router` and `endpointPicker` to get Modelplane's
llm-d picker and [default configuration](#picker-configurations). Under
`PrefillDecode`, Modelplane labels each engine's pods `llm-d.ai/role: prefill`
or `llm-d.ai/role: decode`, which llm-d's `prefill-filter` and `decode-filter`
select on.

A picker that rejects its configuration never becomes ready:

- On a new deployment, the replica doesn't start serving.
- On a running deployment, the old picker keeps serving with the old
  configuration. The replica stops serving, and its endpoint is withdrawn, once
  the picker's rollout passes its progress deadline, 10 minutes by default. The
  new picker's pod log has the reason.

## The sidecar

You write the sidecar as a container beside `engine` in the decode member's
template. A member's template can hold other containers too. Modelplane doesn't
change any container except `engine`. Only the engine gets the `MODELPLANE_`
environment, so Kubernetes doesn't expand `$(MODELPLANE_SERVED_MODEL_NAME)` in
another container's args.

Give the sidecar a `readinessProbe`, so a decode pod isn't Ready until the
sidecar listens. llm-d's sidecar answers `/health` on its port:

```yaml {nocopy=true}
readinessProbe:
  httpGet:
    path: /health
    port: 8000
```

A container beside the engine can also set `resources`, which a namespace with
a `LimitRange` or quota needs.

The sidecar and the picker must match your engines:

| Engine and KV connector | The sidecar's `--kv-connector` | Picker decider |
|---|---|---|
| vLLM with `NixlConnector` | `nixlv2` | Any, such as `prefix-based-pd-decider` |
| vLLM with `MooncakeConnector` | `mooncake` | Any |
| SGLang, any transfer backend | `sglang` | `always-disagg-pd-decider`. An SGLang decode engine rejects a request that skips prefill. |

The sidecar's `--vllm-port` names the decode engine's port, whatever the engine
is.

A decode pod without the sidecar sends every request to decode alone. vLLM
serves it, and SGLang rejects it.

For vLLM with `MooncakeConnector`, set `"kv_connector":"MooncakeConnector"` in
`--kv-transfer-config` on both engines, and leave out the `VLLM_NIXL_*`
environment. The sidecar's `--mooncake-bootstrap-port` must match
`VLLM_MOONCAKE_BOOTSTRAP_PORT` on the prefill engine. Both default to 8998.
vLLM's [Mooncake documentation](https://docs.vllm.ai/en/v0.23.0/features/mooncake_connector_usage.html)
covers the rest, including the image it needs.

SGLang's bootstrap port is 8998 by default too. If you change it on the
engines, set `SGLANG_BOOTSTRAP_PORT` to the same value on the sidecar.

## Engine type

Label SGLang pods `llm-d.ai/engine-type: sglang`, in either serving mode, with
or without `router`. The picker then reads SGLang's metric names. Without the
label, the picker reads vLLM's names and sees a queue depth of zero. vLLM pods
don't need the label.

## Ports

A serving pod receives requests on the serving port, which is 8000 unless you
set `serving.port`. The engine's port named `http` is where Modelplane checks
the engine's health and the collector reads its metrics. It defaults to the
serving port, so an engine alone in its pod declares nothing.

When a container is in front of the engine, such as the sidecar:

- That container declares the serving port, and passes requests to the engine.
  It must pass `/metrics` through too, because the picker reads it there.
- The engine declares its `http` port on another port, such as 8001. The health
  check must reach the engine itself, because the llm-d sidecar answers
  `/health` without asking the engine.

A container that doesn't serve, such as a log shipper, doesn't declare a port,
and the engine doesn't either. Modelplane rejects a `ModelDeployment` with a pod
where more than one container declares the serving port, or where the engine
moves off the serving port and no other container declares it. A `Worker`
member doesn't need a container on the serving port, because worker pods don't
receive requests.

Behind the sidecar, the decode port appears in three places: the `--port` flag
on the engine, its `http` port, and the sidecar flag that names the engine's
port. Keep the three the same. Modelplane doesn't read engine args to check
them.

## Example: SGLang prefill and decode

This deployment serves Laguna-S-2.1 on two 8x H100 nodes, one for each phase.
The engines come from the [Laguna SGLang
recipe](https://github.com/modelplaneai/modelplane/blob/main/docs/manifests/recipes/laguna/model-deployment-sglang.yaml),
with the disaggregation flags from llm-d's [SGLang
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
          template:
            spec:
              containers:
              - name: epp
                image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
                args:
                - --pool-name=$(MODELPLANE_POOL_NAME)
                - --pool-namespace=$(MODELPLANE_POOL_NAMESPACE)
                - --pool-group=inference.networking.k8s.io
                - --config-file=$(MODELPLANE_PICKER_CONFIG)
                - --grpc-port=$(MODELPLANE_GRPC_PORT)
                - --grpc-health-port=$(MODELPLANE_GRPC_HEALTH_PORT)
                - --metrics-port=$(MODELPLANE_METRICS_PORT)
                - --metrics-endpoint-auth=false
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
                readinessProbe:
                  httpGet:
                    path: /health
                    port: 8000
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

## Picker configurations

`Unified` uses this configuration when you don't set `config`. Modelplane sets
`blockSizeTokens` from the engine's `--block-size` or `--page-size`, and uses
at least 64. llm-d's picker raises a smaller value to 64, so its prefix index
doesn't run the picker out of memory.

`maxPrefixTokensToMatch` is set to 131072, llm-d's own default, so the picker
scores prefix matches on the first 128K tokens of a prompt. It doesn't limit the
prompts the engine accepts.

```yaml {nocopy=true}
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    autoTune: false
    blockSizeTokens: 64
    maxPrefixTokensToMatch: 131072
    lruCapacityPerServer: 31250
- type: prefix-cache-scorer
- type: queue-scorer
- type: max-score-picker
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
```

To change it, write the whole configuration in `config`, as
[The endpoint picker](#the-endpoint-picker) describes.

For `PrefillDecode`, this configuration is a starting point for vLLM. Copy it
into `config`, and set `blockSizeTokens` to the engine's block size, or 64 if
that's larger. SGLang needs `always-disagg-pd-decider` in place of
`prefix-based-pd-decider`.

```yaml
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    autoTune: false
    blockSizeTokens: 64
    maxPrefixTokensToMatch: 131072
    lruCapacityPerServer: 31250
- type: prefix-cache-scorer
- type: queue-scorer
- type: prefill-filter
- type: decode-filter
- type: max-score-picker
- type: prefix-based-pd-decider
  parameters:
    nonCachedTokens: 16
- type: disagg-profile-handler
  parameters:
    deciders:
      prefill: prefix-based-pd-decider
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
```

## Upgrading a prefill/decode deployment

Before this release, Modelplane wrote the routing for a `PrefillDecode`
deployment. After the upgrade, a `PrefillDecode` deployment without `router`
and `endpointPicker` stops reconciling. Its `ModelReplica` reports the error,
and its pods and router keep serving as they were. To resume, add:

- `serving.router: EndpointPicker`, with the picker's container and
  configuration in `serving.endpointPicker`.
- The sidecar in the decode member, on the serving port, with the engine's
  `http` port declared.
- `VLLM_NIXL_SIDE_CHANNEL_HOST` and `VLLM_NIXL_SIDE_CHANNEL_PORT` on both
  engines, for vLLM with NIXL.

The [disaggregated example]({{< ref "model-deployment.md#examples" >}}) shows
each one.
