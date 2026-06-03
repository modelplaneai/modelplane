# Code Review: `dennis/drop-kserve-spec`

Reviewed: diff `e899b631..HEAD` (15 commits). Read-only; no branch switch.

## Assessment: APPROVED WITH FOLLOW-UPS

The branch is well-structured, internally consistent, and well-tested. The
KServe→ServingStack rename is clean and behavior-preserving; the native/llm-d
dispatch is sound for the implemented (tensor+pipeline) schema; and the
control-plane↔remote routing convention is coherent end-to-end. The issues
below are mostly follow-ups already half-acknowledged plus one routing concern
that is not covered by the existing TODOs.

---

## Strengths

- **Routing coherence is correct.** compose-model-deployment emits
  `rewritePath = /<ns>/<deployment>/` (raw deployment name + ModelDeployment
  namespace; `fn.py:208`), and both backends emit an HTTPRoute matching
  `/<ns>/<deployment>/` from `replica.metadata.namespace` + the
  `modelplane.ai/deployment` label (native.py:105, llmd.py:213, replica
  `fn.py:_deployment_name` 155-163). The replica is created with the
  deployment's namespace (deployment `fn.py:176`), so both sides agree. Golden
  tests pin both ends (`/ml-team/my-model/` and `/ml-team/my-deployment/`).
- **Dispatch predicate is right for the v0.1 schema.** `needs_cross_pod_coordination`
  is `nodes_per_worker > 1` == `pipeline > 1` (base.py:31-58); tensor-only stays
  native; the `or 1` guard handles `pipeline = None`. Extension points are
  documented no-ops. Tests cover all branches incl. `pipeline=None`.
- **Pydantic access is safe.** `engine_container`'s `next()` is guaranteed by the
  XRD CEL `self.exists_one(c, c.name == 'engine')` (modelreplicas/definition.yaml:93).
  `resource.get_condition(None, ...)` returns Unknown (SDK resource.py:152-158),
  so the readiness logic in replica `fn.py:185-186` is safe when `model-serving`
  is absent.
- **InferencePool scoping is correct.** The pool selector uses BOTH labels
  (`llm-d.ai/inference-serving=true` + `modelplane.ai/serving=<name>`,
  llmd.py:159) so co-located replicas don't cross-select. Test asserts selector
  == LWS pod labels (test_backends.py:166-171).
- **LWS leader/worker templates are deep-copied** (llmd.py:144) — avoids the
  shared-reference mutation trap when the Ray bootstrap follow-up lands.
- **Parallelism-flag injection is idempotent** (llmd.py:79-82); test asserts no
  double-injection (test_backends.py:203-213).
- **Tests are genuinely behavioral**, not tautological: full golden-file
  RunFunctionResponse comparisons for native compose (3 reconcile cases),
  serving-stack (3-pass gating + readiness propagation + gateway-address
  surfacing), and structural assertions for llm-d (v1 field names, no v1alpha2
  remnants, EPP svc/deployment selector match). No vacuous tests found.
- **Clean rename**: no leftover KServe/KEDA references in `functions/`, `apis/`,
  `schemas/`, `examples/` source (grep clean). Project config, flake.nix,
  README, docs, examples all updated consistently. functionRef naming
  (`modelplane-modelplanecompose-serving-stack`) matches the established pattern.

---

## Issues

### Important

1. **Remote HTTPRoute does not strip the `/<ns>/<deployment>/` path prefix
   before the engine.** (native.py:93-113, llmd.py:201-228)
   The control-plane ModelService rewrites the incoming prefix to `rewritePath`
   = `/<ns>/<deployment>/` (an identity rewrite, since match prefix ==
   rewritePath; compose-model-service/fn.py:204-215), so the remote gateway
   receives `/<ns>/<deployment>/v1/...`. The backend HTTPRoute matches that
   prefix but carries **no URLRewrite filter**, so vLLM/SGLang receives the full
   prefixed path and its OpenAI server (which serves `/v1/...`) will 404.
   KServe's internal routing previously absorbed this. The spec (lines 160-178)
   is silent on prefix stripping.
   **Fix:** add a `ReplacePrefixMatch` URLRewrite filter to the backend
   HTTPRoute rule that strips `/<ns>/<deployment>/` down to `/` (so the engine
   sees `/v1/...`). Alternatively, terminate the rewrite to `/` at the control
   plane. Either way, add an end-to-end path assertion. This is the one
   correctness gap not covered by the documented live-cluster TODOs.

### Minor

2. **`ServingStack.spec.versions.gatewayApi` and `.gatewayApiInferenceExtension`
   are dead config.** (servingstack v1alpha1.py:73-86; XRD definition.yaml
   adds both)
   Neither is read anywhere in compose-serving-stack/fn.py. The Inference
   Extension CRDs are loaded from the bundled JSON (a hardcoded version), and
   Gateway API CRDs are not installed by this function at all. These two version
   knobs silently do nothing. Either wire them (e.g. pin the bundled CRD set /
   GAIE install to `gatewayApiInferenceExtension`, install Gateway API CRDs at
   `gatewayApi`) or drop them until they're consumed. Tie this to the bundled
   `inference_extension_crds` refresh TODO (fn.py:53-61) so the version field
   and the CRD payload move together.

3. **EPP Deployment has no readiness/liveness probe.** (llmd.py:171-192)
   The EPP gates all multi-pod traffic via the InferencePool
   `endpointPickerRef`, but its Deployment has no probes, so a wedged EPP still
   reports Ready. Lower priority than the placeholder image/args TODO, but worth
   bundling into the same EPP follow-up.

4. **Redundant `engine_container` call.** replica fn.py caches `self.engine`
   (fn.py:149) and the backend recomputes it (native.py:45 / llmd.py:93). Cheap;
   harmless. Could pass the cached engine into `build()` for clarity. Not
   blocking.

5. **Stale bytecode in `functions/compose-kserve-backend/`.** Tracked source was
   git-renamed to `compose-serving-stack`; only untracked `__pycache__/*.pyc`
   remain on disk. Not in the diff, but `rm -rf functions/compose-kserve-backend`
   would avoid confusion (the dir currently looks like a live function).

---

## Test results

Ran with `PYTHONPATH="<fn>:schemas/python" .venv/bin/python -m unittest discover`.
The five suites touched by this branch all pass; the four untouched suites were
declined by the sandbox before running.

| Suite | Result |
|---|---|
| compose-model-replica (incl. test_backends, 19 tests) | PASS |
| compose-serving-stack (3 tests) | PASS |
| compose-inference-cluster (1 test) | PASS |
| compose-model-deployment (2 tests) | PASS |
| compose-gke-cluster | PASS (OK) |
| compose-inference-class | not run (sandbox declined) |
| compose-inference-gateway | not run (sandbox declined) |
| compose-model-endpoint | not run (sandbox declined) |
| compose-model-service | not run (sandbox declined) |

The four not-run suites are unchanged by this branch.

---

## Rebase-staleness notes (vs origin/main, which diverged ~14 commits)

The branch will conflict in two functions; the renamed one is the harder merge:

- **Inference Extension CRDs JSON→YAML** (main `31d34e63`). main switched
  compose-kserve-backend to `yaml.safe_load_all(... .yaml)`. Our branch keeps
  the JSON loader (serving-stack/fn.py:61) AND ships the stale v1alpha2 JSON.
  The already-flagged CRD-refresh TODO should adopt main's YAML approach and the
  GAIE v1.5.0 set in one move.
- **compose-inference-cluster heavily changed on main** (+182 lines: EKS support
  `e9fc3c52`, `a1a1d15e` "only compose cluster ProviderConfig when kubeconfig
  observed", `a1245df1` dropped GPU XRD fields). Our branch's small rename diff
  there (KServeBackend→ServingStack, drop `KSERVE_VERSION`) will collide; re-apply
  the rename on top of main's version of the file.
- **compose-kserve-backend deletion-order / Usages** (main `d75d72f0`) is
  KServe-specific (KServe CRD vs controller Release ordering) and is deleted
  wholesale by our rename — no real carry-over, but git will show the rename as
  delete+add against main's modified file.
- **EKS / AWS provider deps** (`9a6c7d6c`, `e9fc3c52`) and **control-plane
  Gateway API CRD install + ModelEndpoint RBAC + Traefik gateway Usages**
  (`85cd0105`, `318adda4`, `d154aa7c`) don't overlap our changed files but are
  the surrounding context the rebased branch must coexist with — in particular
  the new `gatewayApi` version field (issue #2) should align with main's
  control-plane Gateway API CRD install version.
