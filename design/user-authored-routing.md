# User-authored routing

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#502](https://github.com/modelplaneai/modelplane/issues/502), [#108](https://github.com/modelplaneai/modelplane/issues/108), [#179](https://github.com/modelplaneai/modelplane/issues/179)

## Summary

A `ModelDeployment` lets the user configure the engines but not the router.
Modelplane configures the router for one case: vLLM with its `NixlConnector`.
An SGLang prefill/decode deployment gets a router it can't use, and reports
Ready while it rejects requests ([#502](https://github.com/modelplaneai/modelplane/issues/502)). Each new KV transfer format needs a
Modelplane release.

I propose the user writes the router for prefill/decode serving:

- `serving.router: EndpointPicker` hands the routing to the user. They write
  the endpoint picker's container and configuration in `serving.endpointPicker`,
  and the sidecar into the decode pod as a container beside `engine` ([#108](https://github.com/modelplaneai/modelplane/issues/108)).
- A `PrefillDecode` deployment must set both. Only `Unified` serving keeps a
  default, where the cluster routes the deployment.

```yaml
serving:
  mode: PrefillDecode
  router: EndpointPicker      # required under PrefillDecode
  endpointPicker:             # required with router
    template:
      spec:
        containers:
        - name: epp
          image: ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0
          args:
          - --pool-name=$(MODELPLANE_POOL_NAME)
          - --config-file=$(MODELPLANE_PICKER_CONFIG)
          # ...
    config: { ... }
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
| vLLM [`NixlConnector`](https://docs.vllm.ai/en/v0.23.0/features/nixl_connector_usage.html) | `nixlv2` | Yes |
| vLLM [`MooncakeConnector`](https://docs.vllm.ai/en/v0.23.0/features/mooncake_connector_usage.html) | `mooncake` | Yes |
| [SGLang](https://docs.sglang.io/docs/advanced_features/pd_disaggregation), any transfer backend | `sglang` | No. Decode [rejects the request](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L2836-L2851) with HTTP 400. |

### What Modelplane composes today

Modelplane configures the router for vLLM with NIXL:

- It injects the sidecar with `--kv-connector=nixlv2`.
- It configures a decider that skips prefill for short or cached prompts.
- It injects `VLLM_NIXL_SIDE_CHANNEL_HOST` and `VLLM_NIXL_SIDE_CHANNEL_PORT`
  into each `PrefillDecode` engine's serving pods.
- It reads `--port` from the decode engine's args, to find the engine behind the
  sidecar.
- It reads `--block-size` or `--page-size` from the engine's args, to configure
  prefix-cache scoring.

The user can't change the picker's configuration or image ([#179](https://github.com/modelplaneai/modelplane/issues/179)).

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
  missing sidecar ([#552](https://github.com/modelplaneai/modelplane/issues/552)). Modelplane can't check these from the API. llm-d's
  picker counts its decisions by type, such as `decode-only`, in
  `llm_d_epp_disagg_decision_total`, which a check could read.
- Routing to data-parallel ranks inside a pod ([#500](https://github.com/modelplaneai/modelplane/issues/500)).

## Proposal

The API changes in three places:

1. `serving.router` names the router the user writes. Its one value is
   `EndpointPicker`. It's required under `PrefillDecode` and optional under
   `Unified`.
2. `serving.endpointPicker` sets the picker's pod template and its
   configuration. It's set exactly when `router` is.
3. A member's template can hold containers beside `engine`. A container can
   declare `ports` and `resources`, and a container beside the engine can set
   a `readinessProbe`.

A `PrefillDecode` deployment writes its routing in the `ModelDeployment`. This
is the decode member of a vLLM deployment with NIXL. The comments mark each part
the user writes:

```yaml
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

The prefill member sets the same environment, without the sidecar. Modelplane
injects neither the sidecar nor the environment. A multi-node decode engine
puts the sidecar in its `Leader` member's template.

Modelplane composes the rest:

- The InferencePool and the HTTPRoute.
- The picker's Deployment, Service, and RBAC, and the environment that tells
  the picker its InferencePool and configuration.
- The `llm-d.ai/role` label on each `PrefillDecode` engine's serving pods,
  from its `phase`. It's the only label Modelplane adds for the picker.
- A memory-backed `/dev/shm` on each engine container.

This is a breaking change to `v1alpha1`. After the upgrade, a `PrefillDecode`
replica without its routing stops reconciling: `compose-model-replica` returns
an error, and leaves the running pods and router in place until the
`ModelDeployment` sets `router` and `endpointPicker`. The deployment must add
the sidecar too, though Modelplane can't check for it. The upgrade also rolls
every replica's picker once, in either mode, because the picker gains a
readiness probe.

### Unified serving

A `Unified` deployment that doesn't set `router` gets llm-d's picker with
Modelplane's default configuration. The configuration reads the block size from
`--block-size` or `--page-size`, and uses at least 64, the smallest llm-d's
picker uses. A `Unified` deployment can set `router: EndpointPicker` with its
own picker to tune it.

### Placement

`router: EndpointPicker` runs only on a cluster that composes an InferencePool
for a picker the user names. Every cluster does, on either serving stack, so
placement doesn't need to check. Stack-aware placement ([#418](https://github.com/modelplaneai/modelplane/issues/418)) adds an
`InferenceCluster.status.routers` list of the routers a cluster can compose,
places a deployment that sets `router` only on clusters that list it, and
reports `ReplicasScheduled=False` when none qualify.

### The endpoint picker

`serving.endpointPicker.template` is the picker's pod, a curated subset of a
pod template like a member's, with one container. The user writes the image
and the command line, because each picker spells its flags differently.
Modelplane passes what only it knows as environment, as the base design does
for an engine, including the ports it expects the picker to serve on:

| Variable | Value |
|---|---|
| `MODELPLANE_POOL_NAME` | The InferencePool the picker serves |
| `MODELPLANE_POOL_NAMESPACE` | The pool's namespace |
| `MODELPLANE_PICKER_CONFIG` | Where Modelplane mounts `config`, when it's set |
| `MODELPLANE_GRPC_PORT` | The port the gateway calls the picker on, 9002 |
| `MODELPLANE_GRPC_HEALTH_PORT` | The port of the picker's gRPC health service, 9003 |
| `MODELPLANE_METRICS_PORT` | The port the picker serves Prometheus metrics on, 9090 |

The ports are the contract. The command line names them through the
environment, so a later release can change one without breaking a deployment's
picker. Modelplane declares them on the container, and the picker's readiness
probe calls the health service. This container runs llm-d's picker:

```yaml
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

Any picker that serves the protocol on those ports fits `router:
EndpointPicker`, such as the Gateway API Inference Extension's lightweight
picker, which takes no configuration. `config` is optional for that reason.
When it's set, it replaces the default configuration whole, and Modelplane
doesn't read it.

The InferencePool selects the replica's serving pods by `modelplane.ai/serving`,
and sends to port 8000. Under `PrefillDecode`, Modelplane labels each engine's
serving pods `llm-d.ai/role: prefill` or `llm-d.ai/role: decode`, which llm-d's
`prefill-filter` and `decode-filter` select on.

A picker that rejects its configuration never becomes ready. A new replica
isn't Ready. A running replica keeps its old picker, which serves the old
configuration, until the new picker's rollout passes its progress deadline.
Then the replica isn't Ready either. Envoy AI Gateway fails closed without a
picker. A picker outage stops requests rather than sending them to prefill
pods.

### Containers beside the engine

This design adopts [#108](https://github.com/modelplaneai/modelplane/issues/108), in either serving mode. A member's template can list
containers beside `engine`:

- Exactly one container is named `engine`, and container names are unique.
- Every container takes `image`, `command`, `args`, `env`, `ports`, and
  `resources`, and startup, readiness and liveness probes. A member may also
  declare ordered init containers, including native sidecars with
  `restartPolicy: Always`.
- Modelplane doesn't change any container except `engine`. It adds device
  claims beside the engine's `resources`, `MODELPLANE_` environment, ModelCache
  mounts and `/dev/shm`. Standard defaults the engine's `http` port and
  readiness probe; authored engine probes replace those defaults. Dynamo
  requires an authored readiness probe on each serving engine.
- A `readinessProbe` sets exactly one of `httpGet`, `tcpSocket`, and `grpc`.
- Under `PrefillDecode`, Modelplane warns when the decode pod has no container
  beside the engine. It can't tell whether a container is the sidecar.
- Each member has its own template. The user chooses which pods of a gang get a
  container.

### Ports

A serving pod receives requests on port 8000, as today. The engine's port named
`http` is where Modelplane probes the engine and the collector scrapes its
metrics. On Standard the collector reads that port on the `engine` container, so a
sidecar port named `http` isn't scraped. The `http` port defaults to 8000, so an
engine alone in its pod declares nothing, and an unnamed 8000 on the engine is
its `http` port.

When a container is in front of the engine, such as the sidecar:

- That container declares port 8000, and passes requests to the engine. It must
  pass `/metrics` through too, because the picker reads it there.
- The engine declares its `http` port on another port, such as 8001. The probe
  must reach the engine itself, because the llm-d sidecar answers `/health`
  without asking the engine.

A container that doesn't serve, such as a log shipper, doesn't declare a port,
and the engine doesn't either. Modelplane rejects a serving pod where more than
one container declares 8000, or where the engine moves off 8000 and no other
container declares it. A `Worker` pod doesn't receive requests. Its ports
pass through as written.

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

These router settings must match the engines:

| Router setting | Why |
|---|---|
| `--kv-connector=sglang` on the sidecar | The engines are SGLang. |
| `always-disagg-pd-decider` in the picker configuration | An SGLang decode engine rejects a request that skips prefill. |
| `llm-d.ai/engine-type: sglang` on the pods | The picker reads SGLang's metric names. Without it, the picker sees a queue depth of zero. |
| `blockSizeTokens: 64` in the picker configuration | llm-d's SGLang guidance: 64, or the engine's page size if it's larger. |

## Future improvements

### Dynamo

`serving.router: Dynamo` selects Dynamo clusters and requires
`serving.dynamo.frontend`, with a user-authored PodTemplate and configurable
replica count. The frontend handles Unified and PrefillDecode traffic. There is
no endpoint picker in that path, and router mode, image, environment and launch
arguments belong to the frontend template's author.

Native engines connect through a user-authored `runtime` init sidecar with
`restartPolicy: Always`. Modelplane preserves engine launches instead of
requiring `python3 -m dynamo.<backend>`. A compute-only Worker may omit the
runtime sidecar. The sidecar's native gRPC contract and engine compatibility are
part of the deployment's stack configuration.

PrefillDecode requires an explicit router on either stack. The API validates
that EndpointPicker has an endpointPicker template and Dynamo has a frontend;
placement filters both retained and new replicas to the router's stack.

### More container fields

Containers can't mount volumes. The sidecar runs with `--secure-proxy=false`,
because `--cert-path` has nothing to point at. The API
can add `volumeMounts` to all containers at once, and `securityContext` with
it.

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

### Modelplane writes the picker's command line

Modelplane could run the image the user names with llm-d's command line, and
take only `image` and `config`. A deployment would write less, and Modelplane
could change a flag for every deployment at once. The command line is llm-d's
though, and it changes between releases: llm-d-router v0.9.0 added
`--grpc-health-port`. The Gateway API Inference Extension's lightweight picker
has no `--config-file` or `--metrics-endpoint-auth` flag, and would exit on
that command line. `router: EndpointPicker` would then mean llm-d's picker,
and a picker with other flags would need a Modelplane release.

### A router named for llm-d

`router: LLMD` would match the contract that a fixed command line hard-codes. It
would be accurate, but every other picker would need its own `router` value,
and its own release, even when it speaks the same protocol on the same ports.
