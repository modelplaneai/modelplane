# Getting-started demo — serve a model, then scale it across a fleet

A two-part getting-started story, in matching **GKE** and **EKS** tracks:

- **Part 1 — first deployment** (`*/01-first-deployment.yaml`): one cheap L4
  cluster, one small model (`Qwen2.5-0.5B-Instruct`), one OpenAI endpoint.
- **Part 2 — scale to fleet** (`*/02-scale-to-fleet.yaml`): the platform adds two
  bigger GPU clusters in different regions; the ML team **edits the same
  deployment in place** — more replicas and a bigger-GPU selector — and the DRA
  scheduler places it by **capability**, skipping the L4. Same model, same
  endpoint, no cluster names, no region labels.

The headline: *the ML team asks for the hardware its model needs, Modelplane
finds it across the fleet — and the endpoint never changes.*

## Layout

| Path | What |
|---|---|
| `gke/01-first-deployment.yaml` | GKE: L4 class + cluster + `qwen-demo` + `qwen` ModelService |
| `gke/02-scale-to-fleet.yaml` | GKE: A100-40 class + two clusters + the in-place deployment edit |
| `gke/first-deployment.md`, `gke/scale-to-fleet.md` | The GKE walkthrough docs |
| `gke/record.sh` | Self-playing screencast for the GKE track |
| `eks/01-first-deployment.yaml` | EKS: L4 class + cluster + `qwen-demo` + `qwen` ModelService |
| `eks/02-scale-to-fleet.yaml` | EKS: L40S class + two clusters + the in-place deployment edit |
| `STORY_ARC.md` | The two-part narrative for the guide and the demo video |

> **GPU notes.** GKE uses single-GPU A100-40 (`a2-highgpu-1g`); GKE has no 1-GPU
> H100 node. EKS uses single-GPU L40S (`g6e.xlarge`); there is no single-H100 EC2
> instance (`p5.48xlarge` is 8× H100). Both are the right cheap "bigger" tier for
> a getting-started guide. The capability story is identical — only the GPU and
> the `memory >=` threshold differ (`>= 35Gi` on GKE, `>= 40Gi` on EKS).

## Recording the 3-minute video (GKE track)

Provision and warm **everything ahead of time**, then record only instant
commands — `gke/record.sh` steps through them so nothing waits on infra on
camera.

### Pre-flight (off-camera)

1. **Quota:** Part 1 needs L4 in `us-central1`; Part 2 needs A100-40
   (`a2-highgpu-1g`) in `us-central1` + `us-east1`. A100-80/H100 had **zero**
   quota in `crossplane-playground`. A100 capacity is per-zone — if a zone is
   stocked out the node pool hangs in `PROVISIONING`; retarget the pool's `zones`
   to one with capacity (quota is regional and unaffected).
2. **Provision + deploy:**
   ```bash
   CP=gke_crossplane-playground_us-central1-a_modelplane-cp
   kubectl --context $CP create namespace ml-team --dry-run=client -o yaml | kubectl --context $CP apply -f -
   kubectl --context $CP apply -f gke/01-first-deployment.yaml
   kubectl --context $CP apply -f gke/02-scale-to-fleet.yaml
   ```
   Wait for all three `InferenceCluster`s and the `qwen-demo` deployment (2/2) to
   report `Ready`.
3. **Warm the endpoint:** send one throwaway `curl` to the `qwen` ModelService
   address so vLLM's first-request latency doesn't show on camera.
4. **Set up the terminal:** `cd examples/getting-started/gke`, export `CP`,
   confirm `jq` is installed.

### On camera

`gke/record.sh` types and runs each command itself with reading pauses, so you
start it, screen-capture the terminal, and **voice over afterward**:

```bash
cd examples/getting-started/gke && ./record.sh
```

Tune pacing with `READ_PAUSE` (seconds after each output, default 6) and
`TYPE_SPEED` (seconds/char, default 0.03). Dry-run with `STEP=1 ./record.sh` to
advance on Enter. The `kubectl get modelreplica` beat is the climax — it shows
the model placed on both A100 clusters and **not** the L4, purely from the
capability selector.

### Teardown

```bash
CP=gke_crossplane-playground_us-central1-a_modelplane-cp
kubectl --context $CP -n ml-team delete modeldeployment,modelservice --all
kubectl --context $CP delete inferencecluster --all --cascade=foreground
```
