# Modelplane All-Hands Demo (~4 min)

## 10 minutes before the demo

```bash
./demo/cleanup.sh
./demo/setup.sh

# Pre-deploy the two models that fit:
kubectl apply -f demo/manifests/deploy-qwen.yaml
kubectl apply -f demo/manifests/deploy-llama-70b-awq.yaml
kubectl wait md --all -n ml-team --for=condition=Ready --timeout=600s
```

Leave the **70B and 405B for live** — those are the rejections.

Start the UI:

```bash
# Terminal 1 — proxy (predemo.sh already started the port-forward)
cd ui && MODELPLANE_GATEWAY_OVERRIDE=http://localhost:8888 go run ./cmd/proxy/ --kubeconfig ~/.kube/config

# Terminal 2 — frontend
cd ui/frontend && npm run dev
```

Open http://localhost:5173 — Deployments page should show Qwen and Llama 8B
AWQ both green.

---

## Live Demo

### [0:00] Intro (15s)

> "Modelplane is a control plane for AI models. Platform teams curate a
> model catalog and manage GPU infrastructure. ML teams deploy from the
> catalog and get back a working endpoint. Let me show you."

---

### [0:15] The Catalog (40s)

**Navigate to Catalog** (`/admin/catalog`)

Four models visible. Point at each row:

> "The platform team has registered four models. Qwen 0.5B — tiny, 2 gig
> of VRAM. Llama 8B quantized with AWQ — 5 gig. Llama 70B full precision
> — 140 gig. And Llama 405B — 810 gig, the biggest open model out there."

**Click into `llama-3-1-8b-awq`** briefly — show the serving profile with
`--quantization awq` and `--max-model-len 8192`.

> "Each model has serving profiles that configure the engine. These flags
> flow straight through to vLLM. ML teams don't touch this."

**Navigate to Environments** (`/admin/environments`)

> "We have one inference environment — a GKE cluster with a single L4 GPU,
> 24 gig of VRAM."

---

### [0:55] The Rejections (45s)

**Navigate to Deploy page** (`/deploy`)

> "Now I'm an ML engineer. Let me try deploying the 70B."

Select `llama-3-1-70b` → Deploy. **Immediate redirect to detail page —
status shows error.**

> "Zero placements. 140 gig needs six L4s, we have one. Rejected."

**Back to Deploy** (`/deploy`)

> "What about the 405B?"

Select `llama-3-1-405b` → Deploy. **Same thing — error.**

> "810 gig needs 34 L4 GPUs. That's a multi-node H100 job. Our L4
> cluster can't touch it."

**Navigate to Deployments page** (`/deployments`) — all four visible.

> "Two rejected, two running. The scheduler checked every model against
> every environment — backend compatibility, GPU capacity, VRAM — and
> made the call *before* anyone burned GPU time."

---

### [1:40] The Working Deployments (40s)

**Click into `llama-8b-awq`** — green status, endpoint URL, placement card.

> "The 8B quantized model — 5 gig of VRAM, fits on our L4. The scheduler
> matched it, placed it, and I get an OpenAI-compatible endpoint routed
> through Envoy Gateway."

Point at the placement card:

> "KServe backend, vLLM engine, 1 GPU. All composed automatically."

---

### [2:20] Chat (40s)

**Scroll to the chat widget.**

Type: **"Explain Kubernetes in one sentence."**

Wait for streaming response.

> "That's hitting Llama 8B quantized — AWQ 4-bit, served by vLLM, on a
> GKE L4 GPU, routed through the control plane. Any OpenAI SDK client
> works with this URL."

**Optional if time:** go back to Deployments, click into `qwen`, chat
with it too — shows two different models both serving through the same
gateway.

---

### [3:00] The Big Picture (30s)

> "Four models, one cluster. Two fit, two don't. Modelplane figured it
> out. If we added an H100 cluster, the 70B and 405B could be placed
> there — same catalog, same API, the scheduler does the rest.
>
> Platform teams get a catalog with capacity guardrails. ML teams get
> self-service deployment. Open source, Apache 2.0."

---

### [3:30] What's Next (20s)

> "Today this is `kubectl apply`. The `mp` CLI is coming — this becomes
> `mp deploy qwen`. We're also adding more backends, more cluster
> sources, and intent-based deployment where you just say what model you
> want and Modelplane figures out the rest."

---

## Retry

```bash
./demo/cleanup.sh
./demo/setup.sh
kubectl apply -f demo/manifests/deploy-qwen.yaml
kubectl apply -f demo/manifests/deploy-llama-70b-awq.yaml
kubectl wait md --all -n ml-team --for=condition=Ready --timeout=600s
# ~5-10 min, then go live
```

## Pre-flight checklist

- [ ] `kubectl config current-context` → `kind-modelplane`
- [ ] `kubectl get ie` → gke-us-central READY
- [ ] `kubectl get ig` → default READY
- [ ] `./demo/cleanup.sh && ./demo/setup.sh` clean
- [ ] Pre-deploy Qwen + Llama 8B AWQ, both READY
- [ ] 70B and 405B **not** deployed (those are live)
- [ ] Chat works on both pre-deployed models
- [ ] UI open to Deployments page (two green rows)
