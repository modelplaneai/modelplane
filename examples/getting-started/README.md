# Getting-started demo (GKE)

A self-playing screencast of the getting-started guide, for recording the
~3-minute intro video. It runs the **exact manifests the guide ships** — straight
out of [`docs/manifests/getting-started/`](../../docs/manifests/getting-started/) —
so the demo and the docs can never drift. The narrative is the guide's:

- **Part 1 — first deployment:** one cheap L4 cluster (`starter`), one small
  model (`Qwen2.5-0.5B-Instruct`), one OpenAI endpoint (`qwen`).
- **Part 2 — scale across the fleet:** the platform team adds two A100 clusters
  in other regions (`gpu-us-west`, `gpu-us-east`); the ML team adds a second
  deployment `qwen-west` pinned to us-west with a bigger-GPU selector, and one
  `ModelService` fronts both `qwen-demo` and `qwen-west` from the same endpoint.

The full prose lives in the guide — [Deploying a model](../../docs/content/getting-started/deploying-a-model.md),
[Scale the platform](../../docs/content/getting-started/scale-the-platform.md),
[Scale the model](../../docs/content/getting-started/scale-the-model.md). This
directory only holds the recorder.

> **Recording assets are stale.** `gke/story-arc-demo.{cast,gif,svg}` and
> `gke/story-arc-transcript.txt` were recorded against the earlier "edit one
> deployment in place" flow and are pending regeneration against the
> docs-aligned flow above (`gke/record.sh`). Re-record when GPU capacity is handy.

## Recording the video

Provision and warm **everything ahead of time**, then record only instant
commands — `gke/record.sh` steps through them so nothing waits on infra on camera.

### Pre-flight (off-camera)

1. **Quota / capacity:** Part 1 needs L4 in `us-central1`; Part 2 needs A100-40
   (`a2-highgpu-1g`) in `us-west1` + `us-east1`. A100 capacity is per-zone — if a
   zone is stocked out the node pool hangs in `PROVISIONING` with a
   `ZONE_RESOURCE_POOL_EXHAUSTED` error; retarget the pool's `zones` to one with
   capacity (quota is regional and unaffected).
2. **Provision + deploy** (set your GCP project; the cluster manifests carry a
   `my-gcp-project` placeholder):
   ```bash
   CP=gke_crossplane-playground_us-central1-a_modelplane-cp
   MF=../../../docs/manifests/getting-started   # from examples/getting-started/gke
   kubectl --context $CP create namespace ml-team --dry-run=client -o yaml | kubectl --context $CP apply -f -
   for f in gke/platform.yaml gke/platform-scale.yaml; do
     sed 's/my-gcp-project/<your-gcp-project>/' "$MF/$f" | kubectl --context $CP apply -f -
   done
   kubectl --context $CP apply -f $MF/gke/model-deployment.yaml      # qwen-demo (L4)
   kubectl --context $CP apply -f $MF/gke/model-deployment-west.yaml # qwen-west (A100, us-west)
   kubectl --context $CP apply -f $MF/model-service-multi.yaml       # qwen → both
   ```
   Wait for all three `InferenceCluster`s and both deployments to report `Ready`.
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
`qwen-demo` on the L4 `starter` and `qwen-west` on the A100 `gpu-us-west`,
fronted by one endpoint.

### Teardown

```bash
CP=gke_crossplane-playground_us-central1-a_modelplane-cp
kubectl --context $CP -n ml-team delete modeldeployment,modelservice --all
kubectl --context $CP delete inferencecluster --all --cascade=foreground
```
