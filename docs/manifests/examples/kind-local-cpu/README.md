# Capability scheduling on a local kind fleet (CPU, no GPU)

Show off the core of Modelplane — **a model lands on the hardware it asks for and
skips the cheaper option** — on a laptop, with no cloud and no GPUs.

The trick: a local kind cluster with two node pools that **declare** different
GPU sizes (`fake-l4` = 24 Gi, `fake-a100` = 40 Gi), both backed by one **mock**
DRA driver. The sizes are declarative, so the scheduler matches a model's memory
selector against them; the engine runs on CPU.

Two clusters:
- **Control plane** — a kind cluster running Crossplane + the Modelplane
  Configuration (from the getting-started install).
- **Workload** — a second kind cluster this example creates, with the two pools.

## 1. Create the inference cluster

```bash
./create-inference-cluster.sh
```

This creates a 2-worker kind cluster, installs the mock DRA driver and MetalLB
(the workload's gateway needs a LoadBalancer IP, which kind has no cloud LB to
provide), labels one worker `modelplane.ai/pool=fake-l4` and the other
`fake-a100`, and registers the cluster's kubeconfig as a Secret on the control
plane.

## 2. Publish the classes + deploy the model (against the control plane)

Creating the workload cluster in step 1 left kubectl pointed at it, so pin the
control-plane context explicitly here — these resources belong on the control
plane, not the workload:

```bash
kubectl --context kind-crossplane-modelplane create namespace ml-team \
  --dry-run=client -o yaml | kubectl --context kind-crossplane-modelplane apply -f -
kubectl --context kind-crossplane-modelplane apply -f .   # two InferenceClasses, the Existing InferenceCluster, a ModelDeployment, a ModelService
```

`qwen-fleet` asks for `>= 35Gi`. Modelplane installs the serving stack on the
workload (~2 min), then schedules the replica onto **fake-a100** and **skips
fake-l4**.

## 3. See the placement

```bash
kubectl --context kind-crossplane-modelplane -n ml-team get modelreplica -l modelplane.ai/deployment=qwen-fleet \
  -o custom-columns='REPLICA:.metadata.name,POOL:.spec.engines[0].members[0].nodePoolName,READY:.status.conditions[?(@.type=="Ready")].status'
# POOL is fake-a100 — the model skipped the 24Gi fake-l4 pool.
```

Change the selector to `>= 20Gi` and either pool is eligible; `>= 41Gi` and
nothing matches (the deployment reports `InsufficientCapacity`).

## 4. Call it

The workload's gateway gets a MetalLB address that isn't routable from your
host, so port-forward the engine pod:

```bash
kubectl --context kind-mp-fleet -n default port-forward \
  "$(kubectl --context kind-mp-fleet -n default get pods -o name | grep qwen-fleet | head -1)" 8000:8000 &

curl -s http://localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"Say hi in 5 words."}],"max_tokens":40,"chat_template_kwargs":{"enable_thinking":false}}' \
  | jq -r '.choices[0].message.content'
```

## Notes

- **Mock sizes are declarative.** The fake-l4/fake-a100 capacities live in the
  InferenceClasses; the mock devices carry no real VRAM. The scheduler matches
  the model's `device.capacity[...].memory` selector against the declared values.
- **CPU inference is slow.** Keep the model tiny (0.6B here). For development,
  not throughput.
- Modelplane also installs the NVIDIA DRA driver with the serving stack; on a
  GPU-less node it sits idle and doesn't interfere with the mock driver.
- This is a dev convenience, not a supported production topology.
