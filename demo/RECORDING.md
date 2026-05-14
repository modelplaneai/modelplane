# Recording Guide

## Step 1: Get the environment ready (~10 min)

```bash
cd /Users/dramdass/work/modelplane
./demo/predemo.sh
```

Wait for "PRE-DEMO READY" message.

## Step 2: Start the UI

```bash
# Terminal 1 — Go proxy (needs the gateway override for local kind)
cd /Users/dramdass/work/modelplane/ui && MODELPLANE_GATEWAY_OVERRIDE=http://localhost:8888 go run ./cmd/proxy/ --kubeconfig ~/.kube/config

# Terminal 2 — React frontend
cd /Users/dramdass/work/modelplane/ui/frontend && npm run dev
```

Open http://localhost:5173 in Chrome. Verify:
- Deployments page shows Qwen + Llama 8B AWQ, both green
- Click into Llama 8B AWQ → chat widget works

## Step 3: Record silently in Loom

Set Loom to screen-only (no camera, no mic). Follow the shot list below.
Move slowly and deliberately — you'll speed up boring parts in editing.

---

## Shot List (silent recording, ~70s)

| # | Action | Hold |
|---|--------|------|
| 1 | **Deployments page** — two green rows visible | 3s |
| 2 | Click **Catalog** in nav | 5s |
| 3 | Click into **llama-3-1-8b-awq** — show VRAM, serving profile, `--quantization awq` args | 5s |
| 4 | Click **Environments** in nav — show gke-us-central, KServe, L4 GPU | 3s |
| 5 | Click **Deploy** in nav — select **llama-3-1-70b** (click the card) | 3s |
| 6 | Click **Deploy** button | 2s |
| 7 | Deployment detail — red status, 0 placements, InsufficientCapacity | 5s |
| 8 | Click **Deploy** in nav — select **llama-3-1-405b** (click the card) | 3s |
| 9 | Click **Deploy** button | 2s |
| 10 | Deployment detail — red status, 0 placements | 4s |
| 11 | Click **Deployments** in nav — all 4 rows: 2 green, 2 red | 5s |
| 12 | Click into **llama-8b-awq** — green dot, endpoint URL, placement card | 5s |
| 13 | Scroll to chat widget — type: **Explain Kubernetes in one sentence.** | 8s |
| 14 | Wait for streaming response to finish | 5s |
| 15 | Scroll up to show endpoint URL one more time | 3s |
| 16 | Click **Deployments** in nav — hold on the 4-row table | 4s |

---

## Step 4: Voice over in Loom editor

Play the recording and talk over each shot. Script below — timestamps are
approximate, adjust to your actual footage.

---

## Voiceover Script

**[Shot 1 — Deployments page, 2 green rows]**

> Modelplane is the open source control plane for AI models. It manages
> model deployment across GPU clusters so platform teams and ML teams
> don't have to build all of that from scratch. Let me show you how it
> works.

**[Shot 2 — Catalog page]**

> Platform teams register models in a catalog. We have four here — Qwen
> half a billion parameters, Llama 8B quantized with AWQ, Llama 70B full
> precision, and Llama 405B — the biggest open model out there. Each one
> declares its VRAM requirements and supported backends.

**[Shot 3 — Llama 8B AWQ detail]**

> The platform team also configures serving profiles. This one uses vLLM
> with AWQ quantization and a max model length — these engine flags flow
> straight through to the container. ML teams don't touch any of this.
> They just pick from the catalog.

**[Shot 4 — Environments page]**

> We have one inference environment — a GKE cluster with a single L4 GPU.
> 24 gig of VRAM. Modelplane provisioned the entire cluster — VPC, node
> pools, KServe, everything — from a single resource.

**[Shots 5-7 — Deploy 70B, rejected]**

> Now I'm an ML engineer. I want to deploy the 70B model. I select it,
> hit deploy, and... zero placements. The scheduler calculated that 140
> gig of VRAM needs six L4 GPUs. We only have one. It rejected the
> deployment before burning any GPU time.

**[Shots 8-10 — Deploy 405B, rejected]**

> Same story with the 405B. 810 gig of VRAM — that's a multi-node H100
> job. Our L4 cluster can't touch it. Clear signal: insufficient capacity.

**[Shot 11 — Deployments table, 2 green + 2 red]**

> Four deployments. Two rejected, two running. The scheduler checked
> every model against every environment — backend compatibility, GPU
> capacity, VRAM — and made the call instantly.

**[Shot 12 — Llama 8B AWQ detail, green]**

> The 8B quantized model fits — 5 gig on a 24 gig GPU. The scheduler
> matched it, placed it, and I get a unified OpenAI-compatible endpoint
> routed through Envoy Gateway on the control plane.

**[Shots 13-14 — Chat]**

> And I can talk to it right here. That's hitting the live model — Llama
> 8B, AWQ quantized, served by vLLM, on a GKE L4 GPU. Any OpenAI SDK
> client works with this endpoint.

**[Shots 15-16 — Endpoint + Deployments table]**

> Platform teams get a model catalog with GPU capacity guardrails. ML
> teams get self-service deployment. If we added an H100 cluster, those
> rejected models could be placed there automatically — same catalog,
> same API.
>
> Modelplane is open source, Apache 2.0. The mp CLI is coming to make
> this even simpler. Thanks.

---

## After recording

```bash
# Clean up to save GKE costs when done practicing:
./demo/cleanup.sh
```

## Troubleshooting

**Kind cluster gone after sleep?**
```bash
docker start modelplane-control-plane
# Wait 30s for API server, then:
./demo/predemo.sh
```

**GPU node scaled to zero (predemo.sh takes >10 min)?**
This is normal. The node auto-scales up when a model is deployed.
First run after idle takes ~5 min extra.

**UI proxy won't start?**
```bash
# Kill stale processes
lsof -ti:8080 | xargs kill 2>/dev/null
lsof -ti:5173 | xargs kill 2>/dev/null
# Retry
cd ui && go run ./cmd/proxy/ --kubeconfig ~/.kube/config
```

**Chat returns 404 or error?**
Verify the model is ready: `kubectl get md -n ml-team`
If Llama 8B AWQ shows READY=False, wait or re-run `./demo/predemo.sh`.
