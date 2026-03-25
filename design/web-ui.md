# Modelplane Web UI — Design Document

**Status:** Draft
**Date:** March 2026
**Author:** Nic Cope

## Executive summary

Modelplane needs a web UI for demos. The MVP is a Crossplane Configuration — it
works, but the demo experience is `kubectl apply` and `kubectl get`. That's fine
for validating the resource model, but it's not compelling to an audience that
wants to see a platform, not a terminal. A web UI turns Modelplane from "look at
these CRDs" into "look at this product."

The UI is a single-page app backed by a thin Go proxy. It reads and writes
Modelplane custom resources through the Kubernetes API. There's no separate
database, no separate state, no separate control loop. The proxy serves the
frontend, forwards API calls to kube-apiserver, and relays chat requests to
model inference endpoints. It runs as one pod.

The default view is the ML team experience: browse the model catalog, deploy a
model, watch it come online, talk to it. A separate admin section — reached via
an explicit "Platform Engineering" button — surfaces the infrastructure view:
environments, GPU pools, catalog management. This mirrors how real platforms
work: the consumer experience is the front door, the admin console is behind a
deliberate click.

## Background

The MVP design (`design/mvp-spec.md`) produces a working end-to-end flow. A
platform team creates an InferenceEnvironment and registers a ClusterModel. An
ML team creates a ModelDeployment and gets an OpenAI-compatible endpoint. The
demo script at the end of the spec walks through this with `kubectl`.

The problem is that `kubectl` flattens the experience. Everything looks like
YAML. The relationship between a ModelDeployment and its ModelPlacements is
invisible unless you know to look. Status conditions are a wall of text. The
unified endpoint URL is buried in a jsonpath query. The audience has to trust
that something interesting is happening behind the scenes — they can't see it.

A web UI makes the resource model tangible. Environments are cards with status
indicators and GPU counts. The model catalog is browseable. Deploying a model is
a button click, and you watch the placements appear and go ready in real time.
The endpoint URL is prominent and copy-pasteable. And the closer — chatting with
the model you just deployed, right there in the browser — ties the whole thing
together.

This is a demo-grade UI. It's not a production console. It doesn't need
authentication, RBAC, multi-tenancy, or offline support. It needs to look good,
tell the Modelplane story, and work reliably for a live demo.

## Goals

The UI is successful if:

1. **The demo is self-contained.** Someone can walk through the entire
   Modelplane story — infrastructure, catalog, deployment, inference — without
   leaving the browser.
2. **The resource model is visible.** The relationships between
   InferenceEnvironments, ClusterModels, ModelDeployments, and ModelPlacements
   are clear from the UI. You can see the fan-out from deployment to placements,
   and from placements to environments.
3. **The ML team experience feels like a product.** Browse models, deploy, get
   an endpoint, chat. No YAML, no kubectl, no Crossplane jargon.
4. **The platform team experience is accessible but separate.** Admin concerns
   (environments, catalog management) are one click away but don't clutter the
   default view.
5. **It looks like it belongs to Modelplane.** Same visual identity as the
   marketing site — deep blue-black, purple accents, monospace labels, the
   paper airplane mark. Not a Bootstrap template or generic dashboard kit.

It's explicitly **not** a goal to:

- Support authentication or RBAC (the UI trusts whoever can reach it)
- Work offline or with degraded connectivity
- Support mobile viewports (laptop/desktop only)
- Be extensible or plugin-friendly
- Replace kubectl for day-to-day operations

## Proposal

### Architecture

```
┌─────────┐      ┌──────────────────────┐      ┌─────────────────┐
│ Browser  │─────▶│  Go proxy (one pod)  │─────▶│ kube-apiserver  │
│  (SPA)   │◀─────│                      │◀─────│                 │
└─────────┘      │  /              → SPA │      └─────────────────┘
                 │  /api/k8s/...   → k8s │
                 │  /api/chat/...  → LLM │      ┌─────────────────┐
                 │                      │─────▶│ inference        │
                 └──────────────────────┘      │ endpoint (vLLM)  │
                                               └─────────────────┘
```

The proxy is a single Go binary. It does three things:

1. **Serves the SPA.** The frontend is built at image build time and embedded in
   the binary via `embed.FS`. No separate static file server.
2. **Proxies Kubernetes API calls.** Requests to `/api/k8s/` are forwarded to
   kube-apiserver using in-cluster credentials. The proxy handles auth — the
   browser never sees a bearer token. This sidesteps CORS (kube-apiserver
   doesn't serve CORS headers) and means the frontend doesn't need cluster
   credentials.
3. **Proxies chat requests.** Requests to `/api/chat/` are forwarded to the
   inference endpoint URL from a ModelDeployment's `status.endpoint.url`. This
   avoids CORS issues with the inference gateway (which is on a different IP)
   and supports streaming (SSE for token-by-token responses).

The proxy uses `client-go` for Kubernetes auth and `net/http/httputil` for
forwarding. Watch requests (for real-time status updates) are passed through as
long-lived HTTP streams. The whole thing is ~300 lines of Go.

#### Why a Go proxy instead of direct API access

The browser can't talk to kube-apiserver directly for two reasons: CORS and
credentials. kube-apiserver doesn't set `Access-Control-Allow-Origin`, so
cross-origin `fetch()` calls from the SPA fail. And even if CORS worked, the
browser would need a bearer token, which means either embedding a token in the
SPA (terrible) or running an OAuth flow (overkill for a demo).

The proxy handles both by running in-cluster with a ServiceAccount. The browser
talks to the proxy on the same origin (no CORS), and the proxy authenticates to
kube-apiserver with its mounted token.

#### Deployment

One Deployment, one Service, one ServiceAccount with RBAC:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: modelplane-ui
rules:
- apiGroups: ["modelplane.ai"]
  resources: ["*"]
  verbs: ["get", "list", "watch", "create", "update", "delete"]
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list"]
```

For local development, the proxy can also run outside the cluster using a
kubeconfig file. A `--kubeconfig` flag switches from in-cluster to file-based
auth.

Access is via `kubectl port-forward` or a LoadBalancer Service, depending on the
demo setup.

### API contract

The proxy exposes three route groups. The frontend uses these exclusively — it
never constructs Kubernetes API URLs directly.

#### Kubernetes passthrough: `/api/k8s/`

Strips the `/api/k8s` prefix and forwards to kube-apiserver. Examples:

```
GET  /api/k8s/apis/modelplane.ai/v1alpha1/inferenceenvironments
GET  /api/k8s/apis/modelplane.ai/v1alpha1/clustermodels
GET  /api/k8s/apis/modelplane.ai/v1alpha1/namespaces/ml-team/modeldeployments
GET  /api/k8s/apis/modelplane.ai/v1alpha1/namespaces/ml-team/modeldeployments?watch=true
POST /api/k8s/apis/modelplane.ai/v1alpha1/namespaces/ml-team/modeldeployments
GET  /api/k8s/api/v1/namespaces
```

The frontend constructs the Kubernetes-native path and prepends `/api/k8s`. No
abstraction layer, no REST-to-GraphQL translation. If you know the Kubernetes
API, you know this API.

Watch requests (`?watch=true`) are long-lived HTTP streams. The proxy holds the
connection open and forwards watch events as newline-delimited JSON. The
frontend uses these for real-time status updates (deployment going Ready,
placements appearing, etc.).

#### Chat proxy: `/api/chat/{namespace}/{name}`

Looks up the ModelDeployment `{name}` in `{namespace}`, reads its
`status.endpoint.url`, and proxies the request body to that URL. The response
is streamed back (SSE) for token-by-token display.

```
POST /api/chat/ml-team/qwen-demo
Content-Type: application/json

{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "messages": [{"role": "user", "content": "What is Crossplane?"}],
  "max_tokens": 200,
  "stream": true
}
```

The proxy reads the ModelDeployment once per request to resolve the endpoint.
No caching — it's a demo, not a production gateway.

#### Health: `/healthz`

Returns 200. Used by the readiness probe.

### Navigation and information architecture

The UI has two modes: the ML team view (default) and the platform engineering
view (admin). Switching between them changes the navigation and available
actions but not the underlying data — both views read the same Kubernetes
resources.

#### ML team view (default)

This is what the audience sees first. The nav bar shows:

```
┌──────────────────────────────────────────────────────────────┐
│  ✈ Modelplane              Models    Deployments      [⚙︎]   │
└──────────────────────────────────────────────────────────────┘
```

Two top-level pages:

**Models** — the model catalog. A card grid of ClusterModels (and Models in the
current namespace, if any). Each card shows:
- Model name (e.g., "Qwen 2.5 0.5B Instruct")
- Engine badge (e.g., "vLLM")
- VRAM requirement (e.g., "2 Gi")
- HuggingFace repo link
- A "Deploy" button that opens the deploy flow

This is read-only. The ML team browses what the platform team has made
available.

**Deployments** — the ML team's ModelDeployments. A table showing:
- Name
- Model (from `status.model.name`)
- Environments (e.g., "1/1 ready")
- Endpoint URL (clickable, copy-pasteable)
- Status indicator (creating / ready / error)
- Age

A "Deploy Model" button in the page header opens the same deploy flow as the
model card's deploy button. Clicking a row drills into the deployment detail
view.

**Deployment detail** — the hero page. Shows:
- Deployment name, namespace, model, status
- Placements as cards: one per ModelPlacement, showing environment name,
  status, GPU count, per-placement endpoint
- The unified endpoint URL, prominent and copy-pasteable, with a generated
  `curl` command
- A chat widget (collapsible panel or inline section) that sends requests to
  the deployment's endpoint via the chat proxy

**Deploy flow** — a modal or slide-over triggered from Models or Deployments:
1. Select a model from the catalog (pre-selected if triggered from a model
   card)
2. Set the number of environments (default 1)
3. Optionally set the namespace (default from a namespace picker or hardcoded
   for the demo)
4. Submit → creates a ModelDeployment CR → redirects to the deployment detail
   page, where the user watches it come online

#### Platform engineering view (admin)

Reached by clicking the gear icon in the nav bar. The nav bar changes to:

```
┌──────────────────────────────────────────────────────────────┐
│  ✈ Modelplane    ‹ Back    Environments    Model Catalog     │
└──────────────────────────────────────────────────────────────┘
```

"Back" returns to the ML team view.

**Environments** — a table of InferenceEnvironments with full detail:
- Name
- Status (Ready / Creating, with condition detail on hover or expand)
- Backend (e.g., "KServe")
- Region (from labels or spec)
- Gateway address
- GPU pools (accelerator type, count, per-GPU VRAM)

Clicking a row shows an expanded detail view with:
- The full status conditions
- Composed resource status (GKECluster ready? KServeStack ready?)
- The ProviderConfig name (useful for debugging)
- The namespace where internal resources live

This page is read-only. Creating an InferenceEnvironment takes 15+ minutes —
not a live demo operation.

**Model Catalog** — the same ClusterModels as the ML team's Models page, but
with write actions:
- "Register Model" button opens a form: model name, HuggingFace repo, engine,
  VRAM, image, extra args → creates a ClusterModel CR
- Each model row has edit and delete actions
- This is where the platform team curates the catalog

### Visual design

The UI follows the Modelplane marketing site's visual identity: deep
blue-black backgrounds, purple and cyan accents, grid texture, monospace
labels. It should feel like the console that belongs to
[modelplane.ai](https://modelplane.ai) — the same brand, adapted for
information density and repeated use rather than one-time storytelling.

The marketing site is maximalist: glowing borders, animated planes, scanline
effects. The console dials that back. It keeps the color palette, typography,
and spatial patterns (card borders, grid gaps, monospace labels) but drops the
motion and decorative effects. Think "the marketing site's admin panel."

#### Color palette

Lifted directly from the marketing site's CSS variables:

| Role | Token | Value | Usage |
|------|-------|-------|-------|
| Background | `--bg` | `#070714` | Page background, nav bar |
| Mid background | `--bg-mid` | `#0d0d1f` | Section backgrounds, alternating rows |
| Card background | `--bg-card` | `#0f0f22` | Cards, elevated surfaces, table headers |
| Border | `--border` | `rgba(255,255,255,0.07)` | Dividers, card edges, table rules |
| Border highlight | `--border-hi` | `rgba(173,123,252,0.25)` | Focused/active borders, hover states |
| Primary text | `--text` | `#ffffff` | Headings, primary labels, table values |
| Muted text | `--muted` | `#8b8aae` | Secondary text, descriptions, timestamps |
| Muted highlight | `--muted-hi` | `#b0afcc` | Hovered muted text, slightly emphasized labels |
| Purple (accent) | `--purple` | `#AD7BFC` | Primary buttons, active nav, links, labels |
| Purple highlight | `--purple-hi` | `#C39EFD` | Button hover, emphasized labels |
| Cyan | `--cyan` | `#22d3ee` | Secondary accent, latency/metric highlights |
| Green | `--green` | `#34d399` | Ready/healthy status, success indicators |
| Red (error) | — | `#ef4444` | Error states, destructive actions |
| Gold | — | `#ffcd3c` | Warning states, caution badges |

The marketing site uses purple as the dominant brand color. The console uses
it more sparingly — as the accent for interactive elements (buttons, active
nav items, focus rings) rather than as a background wash. Most of the screen
is `--bg` and `--bg-card` with white text and `--muted` secondary text. Purple
draws the eye to the things you can click.

```css
:root {
  --bg:         #070714;
  --bg-mid:     #0d0d1f;
  --bg-card:    #0f0f22;
  --border:     rgba(255,255,255,0.07);
  --border-hi:  rgba(173,123,252,0.25);
  --text:       #ffffff;
  --muted:      #8b8aae;
  --muted-hi:   #b0afcc;
  --purple:     #AD7BFC;
  --purple-hi:  #C39EFD;
  --cyan:       #22d3ee;
  --green:      #34d399;
  --red:        #ef4444;
  --gold:       #ffcd3c;
}
```

#### Typography

Matching the marketing site:

- **Sans:** Inter, with system fallbacks. The marketing site uses Inter for
  body text. It's ubiquitous but works well at small sizes in tables and forms
  — the console's primary context. Headings use Inter at weight 700–800 with
  tight letter-spacing (`-0.025em` to `-0.03em`), matching the site's headline
  style.
- **Monospace:** DM Mono, with Fira Code and system monospace fallbacks. The
  marketing site uses this for labels, badges, and code blocks. The console
  uses it for the same things: section labels, resource names, endpoint URLs,
  the chat widget, and the `curl` snippet.

Font sizes follow the marketing site's scale: 11px for monospace labels and
badges (with `letter-spacing: 0.12em; text-transform: uppercase`), 13–14px for
table content and body text, 16–17px for descriptions, and `clamp()` values for
page headings.

#### Component patterns

The marketing site establishes a visual vocabulary. The console reuses it in
a more utilitarian way:

**Status indicators.** Small colored dots with optional glow, matching the
marketing site's `.arch-dot-g` and `.arch-live-dot` patterns. Green (`--green`)
for Ready, purple pulse for creating/in-progress, red for error. The green
dot uses `box-shadow: 0 0 8px` for subtle glow, matching the site's live
indicators.

**Cards.** `--bg-card` background, `1px solid var(--border)`, `border-radius:
12px` — the same treatment as the site's `.arch-env` cards. Hover state
transitions to `--border-hi` (purple tint) and a slightly lighter background
(`#121228`), matching the site's `.ent-cap-card:hover`. Used for model catalog
entries, placement summaries, and environment detail.

**Tables.** `--bg` background with alternating `--bg-mid` rows. `1px solid
var(--border)` between rows. Header row uses monospace, uppercase,
`letter-spacing: 0.1em`, `color: var(--muted)` — the exact pattern from the
site's `.bridge-header` and `.ent-table-head`.

**Buttons.** Primary: `linear-gradient(135deg, var(--purple), #7c3aed)`, white
text, `border-radius: 8px`, with the hover lift and glow shadow from
`.btn-primary`. Ghost/secondary: `1px solid var(--border)`, `color:
var(--muted-hi)`, hover transitions to `--border-hi` background tint — the
`.btn-ghost` pattern.

**Badges.** Small rounded pills following the site's badge patterns. Engine
type ("vLLM") uses the cyan badge style (`.arch-engine-txt`). Status ("Ready")
uses the green badge (`rgba(52,211,153,0.08)` background, `1px solid
rgba(52,211,153,0.2)` border, `color: var(--green)`). Resource quantities
("2 Gi", "1 GPU") use the neutral pill style from `.infra-pill`.

**Section labels.** Monospace, 11px, uppercase, `letter-spacing: 0.16em`,
`color: var(--purple)`. This is the site's `.section-label` pattern, used in
the console for page section headers and form group labels.

**The chat widget.** A panel inside the deployment detail page. `--bg-card`
background with `--border` edges. User messages in purple-tinted bubbles
(`rgba(173,123,252,0.08)` background, `--border-hi` border). Assistant
messages in `--bg-mid` bubbles. Monospace font for the message content.
Streaming tokens appear character by character. Input bar at the bottom with
a ghost-style border and a purple send button.

**Grid texture.** The marketing site uses a subtle 80px grid overlay
(`body::after`). The console could use a fainter version (`opacity: 0.2`
instead of `0.5`) or drop it entirely — it's atmospheric on a marketing
page but potentially distracting when you're reading tables. I lean toward
keeping a very faint version: it ties the visual identity together without
competing with content.

**The nav logo.** Uses `icon-white.svg` (the Modelplane mark) at the same
size as the marketing site's nav (`height: 32px`). The mark is the paper
airplane — no wordmark in the nav, since space is limited and the console
context makes the brand obvious.

#### Brand assets

The marketing site repo (`modelplaneai/website`) has the SVG assets we need:

- `public/icon-white.svg` — white paper airplane mark (nav logo)
- `public/logo-inverted.svg` — full wordmark, white on dark (for splash/empty
  states)
- `public/logo.svg` — full wordmark, color on light
- `public/logos/` — partner/vendor logos (HuggingFace, NVIDIA, etc.) that could
  be used in model cards or environment detail

Copy these into `ui/frontend/public/` rather than depending on the marketing
site repo at build time.

### Tech stack

**Frontend:**
- React 18 + TypeScript
- Vite for build tooling
- Tailwind CSS for styling (with the custom color palette as theme extensions)
- No component library — the component count is small enough to build from
  scratch, and it avoids the generic look of off-the-shelf kits
- `@tanstack/react-query` for data fetching and caching
- Native `EventSource` or `fetch` with streaming for watch/SSE support

**Proxy:**
- Go 1.23+
- `client-go` for Kubernetes authentication
- `net/http` + `httputil.ReverseProxy` for proxying
- `embed.FS` for serving the built SPA
- No web framework — the standard library is sufficient for ~5 routes

**Build:**
- Multi-stage Dockerfile: Node (build SPA) → Go (build proxy, embed SPA) →
  distroless (runtime)
- Single image, single binary

### File layout

```
ui/
├── cmd/
│   └── proxy/
│       └── main.go              # Entry point — flags, server setup
├── internal/
│   └── proxy/
│       ├── proxy.go             # HTTP handler, routing
│       ├── kube.go              # Kubernetes API forwarding
│       └── chat.go              # Chat endpoint proxying
├── frontend/
│   ├── src/
│   │   ├── main.tsx             # Entry point
│   │   ├── App.tsx              # Router, layout, nav
│   │   ├── api/
│   │   │   ├── client.ts        # Fetch wrapper for /api/k8s/
│   │   │   ├── watch.ts         # Watch/SSE stream helper
│   │   │   └── chat.ts          # Chat proxy client
│   │   ├── hooks/
│   │   │   ├── useModels.ts     # ClusterModel + Model queries
│   │   │   ├── useDeployments.ts
│   │   │   ├── useEnvironments.ts
│   │   │   └── usePlacements.ts
│   │   ├── pages/
│   │   │   ├── models/
│   │   │   │   └── ModelsPage.tsx
│   │   │   ├── deployments/
│   │   │   │   ├── DeploymentsPage.tsx
│   │   │   │   ├── DeploymentDetail.tsx
│   │   │   │   └── DeployModal.tsx
│   │   │   └── admin/
│   │   │       ├── EnvironmentsPage.tsx
│   │   │       ├── EnvironmentDetail.tsx
│   │   │       ├── CatalogPage.tsx
│   │   │       └── RegisterModelModal.tsx
│   │   ├── components/
│   │   │   ├── StatusDot.tsx
│   │   │   ├── Badge.tsx
│   │   │   ├── Card.tsx
│   │   │   ├── Table.tsx
│   │   │   ├── Button.tsx
│   │   │   ├── Modal.tsx
│   │   │   ├── NavBar.tsx
│   │   │   ├── ChatWidget.tsx
│   │   │   └── CurlSnippet.tsx
│   │   └── styles/
│   │       └── tailwind.css
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   └── vite.config.ts
├── deploy/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── serviceaccount.yaml
│   └── clusterrole.yaml
├── Dockerfile
└── go.mod
```

Everything lives under `ui/` at the repo root, parallel to `apis/`,
`functions/`, and `tests/`. The Go proxy and React frontend are in the same
directory because they ship as one artifact.

## Page wireframes

Text-based wireframes for each page. These show layout and content, not pixel
precision.

### Models page (ML team default)

```
┌──────────────────────────────────────────────────────────────────┐
│  ✈ Modelplane                    Models    Deployments      [⚙︎]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Model Catalog                                                   │
│  Browse models available for deployment                          │
│                                                                  │
│  ┌──────────────────────┐  ┌──────────────────────┐              │
│  │ Qwen 2.5 0.5B        │  │ Llama 3.1 70B        │              │
│  │ Instruct              │  │ Instruct              │             │
│  │                       │  │                       │             │
│  │  vLLM    2 Gi VRAM    │  │  vLLM    140 Gi VRAM  │             │
│  │                       │  │                       │             │
│  │ Qwen/Qwen2.5-0.5B-   │  │ meta-llama/Llama-3.1 │             │
│  │ Instruct              │  │ -70B-Instruct        │             │
│  │                       │  │                       │             │
│  │          [Deploy ▸]   │  │          [Deploy ▸]   │             │
│  └──────────────────────┘  └──────────────────────┘              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Deployments page (ML team)

```
┌──────────────────────────────────────────────────────────────────┐
│  ✈ Modelplane                    Models    Deployments      [⚙︎]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Deployments                                    [Deploy Model]   │
│  ml-team namespace                                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  NAME          MODEL          ENVS   ENDPOINT     STATUS │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │  ● qwen-demo   Qwen2.5-0.5B   1/1   http://...   Ready  │    │
│  │  ◐ llama-prod  Llama-3.1-70B  0/2   —            Creating│    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Deployment detail page

```
┌──────────────────────────────────────────────────────────────────┐
│  ✈ Modelplane                    Models    Deployments      [⚙︎]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ‹ Deployments                                                   │
│                                                                  │
│  qwen-demo                                         ● Ready       │
│  Qwen/Qwen2.5-0.5B-Instruct · ml-team · 3m ago                  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Endpoint                                                  │  │
│  │  http://10.0.0.50/ml-team/qwen-demo/v1/chat/completions   │  │
│  │                                           [Copy] [curl]    │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Placements                                                      │
│                                                                  │
│  ┌─────────────────────────────┐                                 │
│  │ ● demo-us-central           │                                 │
│  │   InferenceEnvironment      │                                 │
│  │   1x GPU · nvidia-l4        │                                 │
│  │   http://34.56.129.3/...    │                                 │
│  └─────────────────────────────┘                                 │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Chat                                           [Collapse] │  │
│  │ ─────────────────────────────────────────────────────────  │  │
│  │                                                            │  │
│  │  You: What is Crossplane?                                  │  │
│  │                                                            │  │
│  │  Assistant: Crossplane is an open source project that      │  │
│  │  extends Kubernetes to manage cloud infrastructure...      │  │
│  │                                                            │  │
│  │ ─────────────────────────────────────────────────────────  │  │
│  │  [Type a message...                           ] [Send ▸]   │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Deploy modal

```
┌──────────────────────────────────────────┐
│  Deploy Model                      [✕]   │
│                                          │
│  Model                                   │
│  ┌──────────────────────────────────┐    │
│  │ qwen-0.5b-vllm                ▾ │    │
│  └──────────────────────────────────┘    │
│  Qwen/Qwen2.5-0.5B-Instruct · vLLM     │
│  2 Gi VRAM                               │
│                                          │
│  Environments                            │
│  ┌──────────────────────────────────┐    │
│  │ 1                              ▾ │    │
│  └──────────────────────────────────┘    │
│  1 environment available                 │
│                                          │
│  Namespace                               │
│  ┌──────────────────────────────────┐    │
│  │ ml-team                        ▾ │    │
│  └──────────────────────────────────┘    │
│                                          │
│               [Cancel]    [Deploy ▸]     │
└──────────────────────────────────────────┘
```

### Environments page (admin)

```
┌──────────────────────────────────────────────────────────────────┐
│  ✈ Modelplane    ‹ Back          Environments    Model Catalog   │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Inference Environments                                          │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  NAME              BACKEND  REGION       GATEWAY   STATUS│    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │  ● demo-us-central  KServe  us-central1  34.56..  Ready  │    │
│  │  ◐ demo-eu-west     KServe  eu-west1     —        Creating│   │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ▸ demo-us-central                                               │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  Status         Ready (all conditions healthy)             │  │
│  │  Backend        KServe v0.16.0                             │  │
│  │  Region         us-central1                                │  │
│  │  Gateway        34.56.129.3                                │  │
│  │  ProviderConfig demo-us-central-kubeconfig                 │  │
│  │  Namespace      ie-demo-us-central                         │  │
│  │                                                            │  │
│  │  GPU Pools                                                 │  │
│  │  ┌────────────────────────────────────────────────────┐    │  │
│  │  │  nvidia-l4        24 Gi VRAM/GPU      1 GPU total  │    │  │
│  │  └────────────────────────────────────────────────────┘    │  │
│  │                                                            │  │
│  │  Conditions                                                │  │
│  │   ● Ready           True   Available                       │  │
│  │   ● ClusterReady    True   ClusterRunning                  │  │
│  │   ● StackInstalled  True   AllReleasesDeployed             │  │
│  │   ● GatewayReady    True   AddressAssigned                 │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Model Catalog page (admin)

```
┌──────────────────────────────────────────────────────────────────┐
│  ✈ Modelplane    ‹ Back          Environments    Model Catalog   │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Model Catalog                                  [Register Model] │
│  Manage the models available for deployment                      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  NAME             MODEL                   ENGINE  VRAM   │    │
│  ├──────────────────────────────────────────────────────────┤    │
│  │  qwen-0.5b-vllm   Qwen/Qwen2.5-0.5B-..   vLLM   2 Gi   │    │
│  │  llama-70b-vllm    meta-llama/Llama-3...   vLLM   140Gi  │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## Demo flow

This is the script for a live demo. It assumes an InferenceEnvironment and
ClusterModel already exist (created via kubectl before the demo, since
environment provisioning takes 15 minutes).

1. Open the UI. You're the ML team. "Here's what my platform team has given
   me."
2. **Models page.** "I can see Qwen 2.5 0.5B is available in the catalog. My
   platform team registered it, pre-configured for vLLM."
3. Click **Deploy** on the Qwen card. The deploy modal opens with the model
   pre-selected.
4. Set environments to 1, namespace to `ml-team`, click **Deploy**.
5. Redirected to the **deployment detail page**. The deployment appears with
   status "Creating." A placement card appears. The audience watches the
   placement go through Creating → Ready in real time (via watch).
6. The endpoint URL appears. Click **Copy**. "That's an OpenAI-compatible
   endpoint."
7. Expand the **chat widget**. Type "What is Crossplane?" The model responds
   with streaming tokens. "I just deployed a model and I'm talking to it."
8. Click the **gear icon** → "Now let me show you what the platform team sees."
9. **Environments page.** "Here's the GKE cluster with KServe and an L4 GPU.
   Modelplane provisioned all of this from a single InferenceEnvironment
   resource."
10. **Model Catalog page.** "And here's where they registered the Qwen model.
    They could add more models here — different sizes, different engines."

Total demo time: ~3 minutes (assuming environment is pre-provisioned).

## Future work

Things the demo-grade UI explicitly doesn't need but a real product would:

- **Authentication.** OAuth2/OIDC integration, likely delegating to the
  cluster's identity provider.
- **Namespace picker.** The demo hardcodes or uses a simple dropdown. A real UI
  would integrate with RBAC to show namespaces the user has access to.
- **Create InferenceEnvironment.** A wizard-style form for provisioning
  environments. Not useful for a demo (too slow) but valuable in a real
  product.
- **Logs and events.** Pod logs from ModelPlacement's underlying workloads.
  Kubernetes events for debugging.
- **Metrics.** Request latency, token throughput, GPU utilization per
  placement. Probably pulled from Prometheus via a separate metrics proxy.
- **Delete confirmation.** The demo UI can get away with immediate deletes.
  A real UI needs confirmation dialogs, especially for environments and
  deployments.

## Alternatives considered

### Terminal UI (TUI)

A curses-style terminal application using something like Bubble Tea (Go) or
Textual (Python). It would match the kubectl-centric workflow and not require
a browser.

The problem is reach. A TUI is compelling for a developer audience but doesn't
work for a product demo to executives, a conference talk, or a blog post
screenshot. The web UI works for all of these. The visual fidelity of a browser
app — custom colors, layout, the chat widget — makes the demo more memorable.

### Existing Kubernetes dashboards

Backstage, Headlamp, or the built-in Kubernetes Dashboard. These can all show
custom resources. The problem is that they show *Kubernetes*, not *Modelplane*.
The audience sees YAML, conditions, events, and labels. They don't see a model
catalog, a deploy button, or a chat widget. Customizing an existing dashboard
to tell the Modelplane story would take as much work as building a focused
SPA, and the result would be less cohesive.

### Server-rendered app (Go templates, HTMX)

A Go server rendering HTML with minimal JavaScript. This avoids the React/Vite
build chain and keeps everything in one language. I'd probably lean this way
for a tool I maintained long-term.

For a demo UI that might be built in a focused session, the React ecosystem
has better component-level productivity. Streaming chat, watch-based live
updates, and modals are all well-trodden paths in React. The build complexity
is contained in the Dockerfile — the developer experience is `npm run dev`.

## Open questions

**Namespace selection.** The deploy flow needs the user to pick a namespace.
Should this be a dropdown populated from the cluster's namespace list? A
freeform text field? Hardcoded to `ml-team` for the demo? The dropdown is most
polished but requires an extra API call and RBAC for namespace listing. For
the demo, a hardcoded default with an optional override might be enough.

**Watch reliability.** Kubernetes watch connections drop and need reconnection
with a `resourceVersion` bookmark. For a demo this probably doesn't matter —
the user can refresh. But if the demo runs for more than a few minutes,
dropped watches would make the UI feel broken. Worth implementing basic
reconnection logic.

**Chat streaming edge cases.** vLLM's SSE streaming format has some quirks
(the final `[DONE]` message, error responses mid-stream). The chat proxy
needs to handle these gracefully. Worth testing against the actual vLLM
endpoint before the demo.
