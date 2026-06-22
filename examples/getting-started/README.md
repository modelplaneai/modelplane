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

The recorded screencast lives next to `record.sh`: `gke/story-arc-demo.cast`
(asciinema, 120×30) and `gke/story-arc-demo.gif`, plus a plain-text
`gke/story-arc-transcript.txt`. Regenerate them after a flow change with:

```bash
cd examples/getting-started/gke
asciinema rec -c ./record.sh --window-size 120x30 --headless --overwrite story-arc-demo.cast
agg --theme monokai --font-size 16 story-arc-demo.cast story-arc-demo.gif
```

## Recording the video

Provision and warm **everything ahead of time**, then record only instant
commands — `gke/record.sh` steps through them so nothing waits on infra on camera.

### Pre-flight (off-camera)

1. **Quota / capacity:** Part 1 needs L4 in `us-central1`; Part 2 needs A100-40
   (`a2-highgpu-1g`) in `us-west1` + `us-east1`. A100 capacity is per-zone — if a
   zone is stocked out the node pool hangs in `PROVISIONING` with a
   `ZONE_RESOURCE_POOL_EXHAUSTED` error; retarget the pool's `zones` to one with
   capacity (quota is regional and unaffected).
2. **Provision + deploy** against the control plane (the local kind CP here; set
   your GCP project — the cluster manifests carry a `my-gcp-project` placeholder).
   `qwen-demo` and `qwen-west` both match a wide-enough GPU, and the scheduler
   spreads + tie-breaks by cluster name, so **sequence the applies** to land
   `qwen-demo` on the L4 `starter`: apply `qwen-west` first (it's pinned to
   `gpu-us-west` and takes that A100), then apply `qwen-demo` once `starter` is
   Ready but the other A100 cluster isn't yet a free candidate.
   ```bash
   CP=kind-crossplane-modelplane
   MF=../../../docs/manifests/getting-started   # from examples/getting-started/gke
   kubectl --context $CP create namespace ml-team --dry-run=client -o yaml | kubectl --context $CP apply -f -
   for f in gke/platform.yaml gke/platform-scale.yaml; do
     sed 's/my-gcp-project/<your-gcp-project>/' "$MF/$f" | kubectl --context $CP apply -f -
   done
   kubectl --context $CP apply -f $MF/gke/model-deployment-west.yaml # qwen-west → gpu-us-west A100
   # wait for starter (L4) Ready, then:
   kubectl --context $CP apply -f $MF/gke/model-deployment.yaml      # qwen-demo → starter L4
   kubectl --context $CP apply -f $MF/model-service-multi.yaml       # qwen → both
   ```
   Wait for all three `InferenceCluster`s and both deployments to report `Ready`,
   and confirm `qwen-demo` landed on `starter` (not an A100).
3. **Warm the endpoint:** port-forward the kind gateway and send one throwaway
   `curl` so vLLM's first-request latency doesn't show on camera (the kind
   MetalLB IP isn't routable from the host; `record.sh` sets up this same
   port-forward itself):
   ```bash
   kubectl --context $CP -n traefik-system port-forward svc/traefik 8080:80 &
   curl -s http://localhost:8080/ml-team/qwen/v1/models >/dev/null
   ```
4. **Set up the terminal:** `cd examples/getting-started/gke`, export `CP`,
   confirm `jq` and `bat` are installed (`bat` syntax-highlights the manifests).

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
CP=kind-crossplane-modelplane
kubectl --context $CP -n ml-team delete modeldeployment,modelservice --all
kubectl --context $CP delete inferencecluster --all --cascade=foreground
```
