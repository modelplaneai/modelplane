# Linting the design-time examples

A small Python script (`lint.py`) walks every YAML under `examples/`,
matches it to the corresponding XRD in `xrds/` by `kind`, and structurally
validates the spec against the OpenAPIV3Schema.

## Run

```bash
python3 design/proposed-modelplane-api/lint.py
```

Exit 0 if all examples validate, 1 otherwise. No deps beyond stdlib + PyYAML.

## What it catches

- Unknown fields (typos, stale field names) where the schema isn't permissive.
- Missing required fields.
- Type mismatches (string vs int vs object, etc.).
- Enum violations (`cluster.source: aws` when the enum is `[GKE, EKS, AKS, Existing]`).

What it does NOT catch (yet):
- Vocab semantics — does an attribute key in `matchAttributes` actually exist in the
  `CapabilityVocabulary`? Is the value within the declared enum? This is a higher-level
  check than the XRD schema can express. Worth adding as a follow-up if useful.
- Cross-resource references — does this `ModelEndpoint` ref a `ModelDeployment` that
  exists in the same namespace? Schema doesn't know.

## Why bother

This is a design preview, not the API yet. Catching obvious schema-shape errors
locally before pushing keeps the review thread focused on substance instead of
"hey, that field doesn't exist." Run it before every push.

## Future

Once the API moves into `apis/`, the same checks will be done by `up project build`
and `up test run` (Crossplane's standard CI path). The script is a stand-in for
that until then.
