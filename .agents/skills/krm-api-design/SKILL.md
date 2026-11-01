---
name: krm-api-design
description: Design and review Modelplane's Kubernetes Resource Model (KRM) APIs - the XRDs under apis/. Use when designing, reviewing, or changing an XRD schema, or when asked how to structure fields in a Kubernetes API.
---

# KRM API Design

Read `engineering-principles` first for how to approach a change at all. This
skill covers the schema.

Modelplane's APIs are Crossplane XRDs under `apis/`, written as YAML OpenAPI v3
schemas. An XRD schema is a thin wrapper on a CRD schema. The [Kubernetes API
conventions](https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md)
apply to both. Cite them rather than stating a preference.

The goal is a great user experience for API consumers. Think about what it's like
to write YAML against this API and read it back six months later, and about
changing it without breaking anyone.

## Design for Evolution, Not Versioning

Above all, **design APIs that never need a new version.**

API designers see version strings like `v1alpha1` and wrongly assume the path
to evolution is introducing `v1alpha2` or `v1beta1`. CRD versions
are two views into the same stored data. All versions must be round-trippable
to the storage version and back without data loss. This means:

- You can rename or move a field across versions.
- You **cannot** add a new required field that doesn't exist in older versions.
- You **cannot** drop a field that an older version required.

Introducing a new version gets you surprisingly little. The better path is
designing an API that can evolve with purely additive, backward-compatible
changes. Kubernetes evolved Deployment v1 for 7+ years without needing v2.

## Terminology

A thing of `kind: Foo` is a **custom resource**. A **CRD** is the *definition*
that allows it to exist. They're different objects and conflating them in code,
docs or review comments confuses readers who are still learning the model.

## Breaking Changes

Adding an optional field is not breaking. Changing a field's type, its casing,
or making it required is. Modelplane is pre-1.0 and nobody needs migrating, so
a breaking change that makes the API more correct is fine. It doesn't need an
alias, a deprecation or a migration path.

Still, design each field so it never needs breaking, using the rules below.
Freedom to break the API today doesn't make a break free later.

## The Core Rules

### Use Required Fields Sparingly

Assume you can never remove a required field. Ask: does the user really
need to express an opinion about this, or can the system pick a sensible default?

If a sensible default exists, make the field optional with an explicit default
value. Explicit defaults are important, because they make the full desired state
visible rather than hiding it behind "unspecified means default behavior".

### Prefer Enums Over Booleans

This is the most common API design mistake. A boolean seems simple today
but almost always evolves into a set of options.

```yaml
# Bad: what happens when you need a third option?
spec:
  autoScaling: true

# Good: room to grow.
spec:
  scalingPolicy: Automatic  # Could later add Manual, Scheduled, etc.
```

Many ideas start as boolean but trend toward a small set of mutually exclusive
options. Use a PascalCase string enum from the start. The cost is trivial (a
string field instead of a bool). The benefit is avoiding a breaking change when
the third option inevitably arrives.

When you see yourself reaching for a bool, stop and think: is there a world
where this becomes three options? If there's even a chance, use an enum.

### If in Doubt, Use an Array

Could a field plausibly need multiple values in the future? Start with an array.

```yaml
# Risky: what if you need multiple regions later?
spec:
  region: us-east-1

# Safer: single-element arrays are fine.
spec:
  regions:
    - us-east-1
```

Worst case, 99% of users use a single-element array forever. If you do need
more, you've saved yourself and your users from a painful migration.

### Leave Room for Variants

This is the most valuable pattern for future-proofing an API, and the one
Modelplane's maintainers raise most. Whenever you add something specific to one
cloud, engine, backend or vendor, ask whether there will be alternatives. There
usually will be. Put the specific fields under a **variant subobject** named for
the variant, and add a required **discriminator field** that says which
subobject applies:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: us-west
spec:
  cluster:
    source: GKE
    gke:
      project: my-project
      region: us-west1
```

Adding EKS is then purely additive, as `source: EKS` with an `eks` object.
Don't infer the variant from which subobject the user happened to set, and don't
add cloud-specific fields to a shared object. An AWS-only field in a generic
`pools` block is the defect this pattern prevents.

This pattern appears throughout well-designed Kubernetes APIs:

| Discriminator | Variants | API |
|---|---|---|
| `mode` | `Pipeline` | Composition |
| `strategy` | `None`, `Webhook` | CRD conversion |
| `source` | `None`, `Secret` | FunctionCredentials |
| `provider` | `Cosign` | ImageConfig verification |

**Choose a good name for the discriminator.** `mode`, `policy`, `strategy`,
`engine`, `backend`, `system` all tell you something about the domain. `type` is
acceptable when nothing more descriptive fits, but reach for a domain-specific
name first.

**The discriminator should be a required enum.** Make the variant subobjects
optional, and add CEL validation rules that require the matching one, as
`apis/inferenceclusters/definition.yaml` does:

```yaml
x-kubernetes-validations:
- rule: "self.source != 'GKE' || has(self.gke)"
  message: spec.cluster.gke is required when spec.cluster.source is GKE.
- rule: "self.source != 'EKS' || has(self.eks)"
  message: spec.cluster.eks is required when spec.cluster.source is EKS.
properties:
  source:
    type: string
    enum: [GKE, EKS]
  gke:
    type: object
  eks:
    type: object
```

Worst case, you never need a second variant and some fields are slightly more
nested than necessary. If you do, you've saved yourself a breaking change.

## Conditions

Conditions are the standard way to communicate status. Get them right.

- **Set conditions on first reconcile, even if status is Unknown.** This lets
  other components know the controller exists and is making progress.
- **Provide a summary condition.** Use `Ready` for long-running resources,
  `Succeeded` for bounded-execution resources. Simple consumers query just this.
- **Name conditions as adjectives or past-tense verbs.** `Ready`, `Available`,
  `Succeeded`, `Failed`, rather than present-tense verbs like `Deploying`.
- **Choose polarity that's clearest for humans.** Which polarity is better
  depends on the condition. `MemoryExhausted` may be clearer than
  `SufficientMemory`. Avoid double negatives like `Failed=False`.
- **Reason is a one-word CamelCase identifier.** `BackendNotFound`, not
  `"the backend could not be found"`. Message is the human-readable sentence.
- **Conditions are a map list.** Use `x-kubernetes-list-type: map` with
  `x-kubernetes-list-map-keys: [type]`.

## Printer Columns

Define `additionalPrinterColumns` so `kubectl get` is immediately useful.

- Surface the most important condition statuses (e.g. `READY`, `SYNCED`).
- Show key spec fields that identify or distinguish instances (e.g. `PACKAGE`,
  `ENGINE`).
- Always include `AGE` as the last column.
- Use the jsonPath `$.status.conditions[?(@.type=='Ready')].status` pattern.

## Field Design

### Naming

- JSON field names are camelCase. Kind names are PascalCase singular. Resource
  plurals are all lowercase.
- Be declarative, not imperative. Name fields for what something **is**, not
  what it **does**.
- Include units when ambiguous: `timeoutSeconds`, `retryLimit`,
  `intervalMinutes`. Use `fooSeconds` for durations.
- Don't abbreviate unless extremely common (`id`, `args`). Acronyms should be
  uniform case: `httpGet` at field start, `TCP` as a constant.
- Reference fields: `fooRef` for object references, `fooName` for by-name
  references. Use `fooSelector` for label-selector-based references.

### Bounds and Validation

Every field needs bounds. This isn't optional. Unbounded fields are a DoS
vector and signal an unfinished API.

- All strings: `maxLength` (and `minLength` if appropriate).
- All numbers: `minimum` and `maximum`.
- All arrays: `maxItems` (and `minItems` if appropriate).
- All maps: `maxProperties`.
- All enums: explicit `enum` list with PascalCase values.
- Use `format` where applicable: `int32`, `int64`, `date-time`, `byte`.
- Use CEL `x-kubernetes-validations` for cross-field constraints, immutability
  (`self == oldSelf`), and conditional requirements.

### Lists

- Use lists of named subobjects, not maps of subobjects. The only exceptions
  are pure key-value maps (labels, annotations, data).
- Declare merge semantics on every list:
  - `x-kubernetes-list-type: map` with `x-kubernetes-list-map-keys` for
    merge-by-key (e.g. conditions by `type`, ports by `containerPort`).
  - `x-kubernetes-list-type: atomic` for replace-wholesale (e.g. enum lists,
    short names).

### References

- Namespaced resources should reference objects in the same namespace. Cross-
  namespace references breach security boundaries. If required, document the
  edge cases and add permission checks.
- For references that could be by-name or by-selector, provide both
  `resourceRef` and `resourceSelector` fields. Document that the ref takes
  precedence when the user sets both.

## Spec and Status

- **Spec holds desired state. Status holds observed state.** They have separate
  update paths via the `/status` subresource.
- **No extra top-level fields** beyond `spec`, `status`, and standard metadata.
  If a resource doesn't need status (its state can't vary from intent), you can
  omit status or rename spec to something domain-appropriate (e.g. `data`).
- **Spec fields are declarative.** They describe what the user wants, not what
  actions to take.
- **Don't put "READ-ONLY" in a status field description.** Users can't write
  to status fields. The note only raises the question of why this one said it.

## CRD Metadata

- Put every resource in a **category** so `kubectl get <category>` works across
  all your resources. Choose a descriptive group-level category.
- Provide **short names** for resources users interact with frequently. Keep
  them lowercase, short, and intuitive.
- Write a **resource-level description** that explains what the resource is in
  one sentence, optionally followed by a link to documentation.
- Write clear **field-level descriptions** that explain semantics, constraints,
  and interactions with other fields, rather than just restating the field's
  name.

## Key Principles

1. Design for evolution, not versioning. You get less from a new version than
   you think
2. Prefer enums over booleans. The third option always arrives
3. Use the discriminator + subobject pattern to leave room for variants
4. Choose descriptive discriminator names: `mode`, `strategy`, `engine` over `type`
5. Start with arrays when multiple values are plausible
6. Use required fields sparingly, and assume they're permanent
7. Put bounds on everything: strings, numbers, arrays, maps
8. Declare list merge semantics explicitly on every list
9. Set conditions on first reconcile, and provide a summary condition
10. Make `kubectl get` useful with printer columns that show conditions and key
    fields
11. A thing of `kind: Foo` is a custom resource. The CRD is its definition
12. Being free to break an API is no substitute for not needing to
