# Getting-started demo — capability scheduling across a fleet

A three-stage story for the getting-started guide and the demo video:

- **Stage 0** (`stage0-single-cluster.yaml`) — one cheap L4 cluster, a 7B model,
  one OpenAI endpoint.
- **Stage 1** (`stage1-fleet-by-capability.yaml`) — the platform adds two
  "expensive" A100 clusters in different regions; a bigger 14B model selects them
  **by GPU capability** (`memory >= 35Gi`), with no `clusterSelector`. It lands on
  the A100 clusters and skips the L4 — the DRA scheduler finding hardware
  fleet-wide.
- **Stage 2** (`stage2-canary.yaml`) — canary a tuned variant safely: a second
  deployment joins the **same `ModelService`** as a second `endpoints[]` entry,
  so the stable:canary split just follows replica counts (2:1). Promote by
  scaling, roll back by deleting — same address throughout, no traffic weights.

The headline: *the ML team asks for the hardware its model needs, Modelplane finds
it across the fleet, and a new variant ships behind the same endpoint — no region
labels, no weights, no tickets.*

## Recording the 3-minute video

Provision and warm **everything ahead of time**, then record only instant
commands (`record.sh` steps through them). Nothing waits on infra on camera.

### Pre-flight (off-camera)

1. **Quota:** this needs A100-40 (`a2-highgpu-1g`) in `us-central1` + `us-east1`.
   A100-80GB / H100 had **zero** quota in `crossplane-playground` — the manifests
   document the swaps if you have it.
2. **Provision + deploy:** set the project, then
   ```bash
   CP=gke_crossplane-playground_us-central1-a_modelplane-cp
   kubectl --context $CP create namespace ml-team --dry-run=client -o yaml | kubectl --context $CP apply -f -
   kubectl --context $CP apply -f stage0-single-cluster.yaml
   kubectl --context $CP apply -f stage1-fleet-by-capability.yaml
   ```
   Wait for all 3 `InferenceCluster`s, the `ModelCache`, and both
   `ModelDeployment`s to report `Ready` (~15–20 min/cluster; the 14B cache stages
   ~28 GB).
3. **Warm the endpoints:** send one throwaway `curl` to each ModelService address
   so vLLM's first-request latency doesn't show on camera.
4. **Set up the terminal:** `cd examples/getting-started`, export `CP`, confirm
   `jq` is installed.
5. **Check VPC network headroom first.** Each `InferenceCluster` provisions one
   VPC network, and a GCP project caps at **50 networks** (`gcloud compute
   project-info describe --format='value(quotas)' | tr ';' '\n' | grep NETWORKS`).
   `crossplane-playground` sat at 50/50, which made every cluster hang Unready
   with subnets 404-ing on a network the MR falsely reported `Ready` (provider-gcp
   doesn't surface the quota rejection). If you hit that, free unused networks or
   raise the `NETWORKS` quota — it is *not* a manifest problem.

### On camera

`record.sh` is a **self-playing screencast** — it types and runs each command
itself with reading pauses, so you just start it, screen-capture the terminal,
and **voice over afterward**:

```bash
./record.sh
```

Tune the pacing for your voiceover with env vars: `READ_PAUSE` (seconds after
each output, default 6), `TYPE_SPEED` (seconds/char, default 0.03). Do a dry run
with `STEP=1 ./record.sh` to advance on Enter instead.

Beat 3's `kubectl get modelreplica -o wide` is the climax — it shows the 14B
placed on both A100 clusters and **not** the L4, purely from the capability
selector. Consider a split-screen with the `cel:` line next to where it landed.

### Teardown

```bash
kubectl --context $CP delete inferencecluster --all --cascade=foreground
kubectl --context $CP -n ml-team delete modeldeployment,modelservice,modelcache --all
```

## Files

| File | What |
|---|---|
| `stage0-single-cluster.yaml` | L4 `InferenceClass` + cluster + 7B `ModelDeployment` + `ModelService` |
| `stage1-fleet-by-capability.yaml` | A100 class + two clusters + 14B `ModelCache`/`ModelDeployment`/`ModelService`, selected by capability CEL |
| `stage2-canary.yaml` | A canary `ModelDeployment` + the `qwen-14b` `ModelService` widened to front both deployments (per-deployment `endpoints[]`) |
| `record.sh` | The self-playing screencast (instant reads + warm curls) |
