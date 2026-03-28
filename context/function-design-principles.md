# Composition Function Design Principles

Modelplane is intended to be the best-in-class reference for how to build complex
Crossplane configurations using Python composition functions. These principles
guide how we write and review function code. Use them as a checklist when
implementing or modifying composition functions.

## 1. The hardest thing should be the domain, not the code

Someone reading a composition function should spend their mental energy
understanding the domain (Crossplane, Kubernetes, inference infrastructure) --
not the code patterns. Use basic Python: classes for grouping state and methods,
functions for reusable logic, if/else for control flow. Avoid metaclasses,
decorators, generics, abstract base classes, or patterns that require
understanding design patterns to read.

Python was chosen for Modelplane because it scales conceptually: a simple
function can look like a config DSL (dicts, models, little flow control), while
a complex function scales up to handle advanced logic in the same language. If
reading a composition function ends up like reading controller-runtime code in
Python, we've failed.

## 2. Make the data flow visible

A reader should be able to trace what data enters a function, how it transforms,
and where it goes -- without reading every line. Shared context (req, rsp, xr)
and required resources belong on the Composer object. Derived values (matched
environments, GPU counts) flow explicitly as method arguments. If you can't tell
what a method depends on from its signature and the Composer's fields, the
boundaries are wrong.

## 3. Separate observation from action

Reading observed state, composing desired state, and deriving conditions are
different activities. They should be separate methods, not interleaved. When you
need to change how a condition is derived, you shouldn't have to read the
resource composition code to find it.

## 4. Make gates explicit

Resource gating -- don't compose resource B until resource A is ready -- is a
core Crossplane composition concept. Gates should be return-early guard clauses
at the top of a method, not conditionals that wrap the body and indent everything
inside them. Compute gate conditions inline rather than storing them on self:

```python
def compose_kserve(self):
    pc_observed = self.provider_configs_observed()
    cert_manager_ready = conditions.has_condition(self.req, "cert-manager", "Ready")
    if not (pc_observed and cert_manager_ready):
        return
    ...
```

When a resource must persist after initial creation even if the gate becomes
transiently false, include the observed-state check in the guard:

```python
def compose_kserve(self):
    if not (gate_satisfied or "kserve" in self.req.observed.resources):
        return
    ...
```

This is a Crossplane-specific pattern that should be visible and commented
inline, not hidden behind a helper.

## 5. Prefer return-early over complex conditionals

When multiple conditions must be checked, prefer sequential guard clauses over
a single compound conditional with mixed polarity. Each guard should be a simple
positive check:

```python
# Prefer this:
if not cert_manager_ready:
    return
if conditions.has_condition(self.req, "kserve-controller", "Ready"):
    return
response.normal(self.rsp, "cert-manager ready, composing KServe")

# Over this:
if (cert_manager_ready
    and not conditions.has_condition(self.req, "kserve-controller", "Ready")
    and "kserve-controller" not in self.req.observed.resources):
    response.normal(self.rsp, "cert-manager ready, composing KServe")
```

## 6. Abstract mechanism, not domain

Helpers for Crossplane mechanics (checking conditions, updating status, setting
conditions on the XR) are good abstractions -- they remove boilerplate without
hiding decisions. Helpers that obscure domain-specific choices (which backends
are compatible, which fields to default, how GPUs are computed) lose context that
the next reader needs.

Test: if the helper requires understanding the domain to know whether it's
correct, it's abstracting at the wrong level.

## 7. Typed over untyped

Use generated Pydantic models and builder functions instead of raw dict literals
where practical. A Pydantic model is self-documenting -- you can see the fields,
types, and structure. A nested dict literal requires reading the target API's
docs to know if it's correct.

When a typed model doesn't exist for a resource (Gateway API types, MetalLB
types), keep the dict inline with a comment rather than wrapping it in a builder
that hides the shape. Self-contained trumps DRY for resource definitions.

## 8. Consistent structure across functions

Every composition function follows the same shape:

```python
class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = XRType(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        # 1. Resolve inputs (required resources, early return if not ready)
        # 2. Compose resources
        # 3. Write status
        # 4. Derive conditions and readiness


def compose(req, rsp):
    Composer(req, rsp).compose()
```

The Composer class and its methods are not prefixed with underscores. The
privateness convention doesn't add value here and is unintuitive to non-Python
readers.

A reader who understands one function can navigate any of them by looking for
the same landmarks.

## 9. One pattern, all scales

Use the same Composer structure for every function -- simple or complex. The
25-line function gets a thin class that looks slightly more ceremonial than
necessary. The 400-line function gets the same class with more methods. The
consistency is worth the small overhead on the simple end.

If a pattern can't scale down to the simplest function without feeling heavy,
it's too much. If it can't scale up to the most complex function without getting
unwieldy, it's too little.

## 10. Optimize for navigation, not brevity

Named methods, consistent ordering, and clear phase boundaries make code longer
but faster to understand. The top-level compose() is for someone asking "what
does this function do?" Each sub-method answers "how does this specific part
work?" Total line count doesn't matter; time-to-understanding does.

## 11. Access fields directly; don't alias them

Use `self.xr.metadata.name` directly instead of assigning it to an intermediate
variable like `name` or `xr_name`. The full path is standard Kubernetes
vocabulary that anyone in the domain recognizes. Intermediate aliases add lines
without adding meaning and hide where the value comes from.

Intermediate variables are appropriate when they hold:
- Computed values: `rewrite_prefix = f"/{ns}/{name}/"`
- Defaulted values: `gw = self.xr.spec.gateway or v1alpha1.Gateway()`
- Derived naming conventions: `pc = _pc_name(self.xr)` where `_pc_name` is a
  module-level function encoding a naming convention

## 12. One function, one concern

Each composition function should map to one clear responsibility from the design
document. If a function is doing two things that change for different reasons
(e.g., cluster provisioning and backend installation), that's a sign it should be
two XRDs with two functions, wired together by composition.

## 13. A little repetition is better than a little indirection

If a helper saves three lines but requires a reader to follow a reference to
another file, keep the three lines. Inline code with a comment is more
approachable than a well-named function in a different module.

Extract only when:
- The same logic appears in multiple functions and could silently diverge
- The extracted code is a Crossplane mechanism, not domain logic (see principle 6)
- The helper genuinely removes a concept rather than adding one

## Checklist

When reviewing a composition function implementation, verify:

- [ ] The compose() method reads like a table of contents for the function
- [ ] req, rsp, xr, and required resources are Composer fields; derived values
      are method arguments
- [ ] Resource composition and condition derivation are in separate methods
- [ ] Gates are return-early guards, not wrapping conditionals
- [ ] Complex conditionals are broken into sequential guard clauses
- [ ] Fields like self.xr.metadata.name are accessed directly, not aliased to
      intermediate variables without good reason
- [ ] A newcomer can understand what Kubernetes resources are composed by reading
      the function alone, without following references to helpers in other files
- [ ] The function follows the standard phase ordering: resolve inputs, compose
      resources, write status, derive conditions
- [ ] Typed Pydantic models are used where available; inline dicts are commented
- [ ] Helpers abstract Crossplane mechanics, not domain-specific decisions
- [ ] The Composer class and methods are not underscore-prefixed
- [ ] The structure matches other composition functions in the project
