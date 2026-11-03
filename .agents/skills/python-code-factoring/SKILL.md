---
name: python-code-factoring
description: Factor and structure Python code for clarity, testability, and maintainability. Use when writing, refactoring or reviewing the structure of Python code, or adding type annotations. Also use when deciding where constants or modules belong, or when the user mentions factoring, refactoring, or code organization in Python. Also use before writing Python unit tests.
---

# Python Code Factoring

Read `engineering-principles` first. It covers how to approach a change, and the
language-agnostic rules for writing less code and for comments and tests. This
skill covers what is specific to Python.

For Crossplane composition functions, `crossplane-python-functions` covers the
compose loop, required resources, readiness and gating.

[references/example.md](references/example.md) has real before and after pairs
for the rules below, taken from the reviews that produced them.

## Write for the Next Reader, Not the Compiler

> "It should be friendly and easy to understand to newbies, even if that means
> doing something that might be a little less DRY or efficient."
>
> "if reading these functions just ends up like reading controller-runtime code,
> but in Python, we've failed."

Put legibility ahead of both DRY and efficiency. The hardest thing in a file
should be the domain rather than the machinery.

Prefer one pattern that scales up and down over two right-sized ones.

## Use the Typed Model

Where a generated or declared model exists for a value, use it. Raw dicts are
the fallback for data that has no model, not a convenience for when the model is
awkward.

```python
# Good.
fs = filesystem.FileSystem.model_validate(observed)
name = fs.metadata.annotations["name"]

# Bad. Untyped, and every key is a typo waiting to happen.
name = to_dict(observed).get("metadata", {}).get("annotations", {}).get("name")
```

The places this gets missed:

- **Reads**, not just writes. Validate into the model, then use attribute
  access. A chain of `.get()` calls is the same defect as building a dict.
- **Tests**, for the XR. Composed and observed resources are the exception:
  tests write them as wire-form dicts, as the Tests section of CONTRIBUTING.md
  explains.
- **Later changes.** Reintroducing a dict is a regression, including in a file
  that already contains one.

When a serialization quirk makes the model awkward, change the schema that
generates it, or drop the field if nothing reads it. Reaching around the model
at the call site is the wrong repair.

## Never Hand-Edit Generated Code

Generated models, clients and schemas are outputs. If one is wrong, fix the
generator or its input and regenerate with the project's own build target. That
includes merge conflicts: resolve a conflict confined to generated files by
regenerating, never by hand.

## Boring Python

Prefer the plain construction a reader can follow without decoding it.

- Free functions and simple classes over inheritance hierarchies. Where several
  modules share a structure, let convention carry it and skip the base class.
- No metaclasses, descriptor tricks or intersection types. A type-level
  construct a reader has to decode is a cost even when it's correct, and many
  boring suppression comments beat one clever type. A `Protocol` is fine when it
  buys something concrete, such as decoupling a helper so it can move upstream.
- No serialization gymnastics where plain assignment works. Round-tripping a
  value through `model_dump` and `model_validate` to reach an identical schema
  is overcomplicating it.
- Comprehension golf needs a comment. Nobody can read
  `max((-(-len(x) // w) for w, x in live), default=1)` without one.

Underscore prefixes on the methods of one entry-point class read as noise to
people who don't work in Python daily. Use them for a real internal boundary
rather than by reflex.

## Names and Locals

**Don't alias an expression that already reads well.** `xr.spec.name` beats
`name = xr.spec.name`, which is three characters. Several call sites don't
rescue a local either. Keep one when it names something the expression doesn't
say, or when it caches an expensive call.

**Guard clauses over nesting.** Return early on each failure rather than
building a compound condition. If a reader has to work out what
`if a and not b and c not in d` means, restructure it into ordered returns.

**Don't accumulate state on `self` to pass it between methods.** Attributes are
for what belongs to the object for as long as it exists, such as the request and
response. A cluster of `self.auth_*` fields that have to remain consistent is
either a value type or, more often, a set of arguments.

**Too many arguments is a smell.** Trace the concrete inputs and outputs of each
step to find the right place to split, and drop any parameter the function can
derive from another one it already has.

## Modules and Constants

Choose a module's name for what it holds, so the module name does half the
naming work at the call site: `status.populate(xr)` reads better than a
`populate_status` that lives wherever was convenient. A name that says nothing
about its contents, `consts` being the usual offender, attracts unrelated code.

A module's name is a contract. `metadata.py` holds Kubernetes object metadata,
so an unrelated constant doesn't go there just because it needs a home.

Extract a module when it has a consumer today.

For constants:

- A string that steers control flow doesn't belong inline. Reuse the generated
  enum value where the schema has one, otherwise make it a constant.
- Make a value that forms a contract between components a constant, but leave
  an ordinary config argument as a literal.
- Declare the constants a module needs at the top of that module, where a reader
  can see its whole vocabulary at once.
- Prefer per-module constants over a shared constants package, and accept the
  duplication. The Crossplane CLI tooling fights a shared package, and symlink
  hacks cost more than the duplication does.
- Never alias an imported constant into a module-local name.

## Imports

Google Python style. Import modules, not the names inside them.

```python
# Good.
from myproject import conditions
c = conditions.Condition(...)

# Bad.
from myproject.conditions import Condition
c = Condition(...)
```

Match the codebase's existing aliases rather than inventing new ones, and use
the conventional ones where they exist, such as `metav1` for `meta.v1`.

## Type Annotations

Annotate function declarations, and have the linter enforce it rather than
relying on review.

Only add annotations a checker actually verifies, and wire that checker into the
project's own tooling. An unchecked annotation is a claim nothing tests.

When the checker complains, first ask whether it's right.

- In library or production code a type error usually reports a design problem.
  A value that is never `None` shouldn't have an `Optional` type. A helper only
  tests call belongs in the tests. Fix the design rather than the annotation.
- A targeted suppression with a stated reason is acceptable, and is better than
  a clever type. Prefer replacing it with a small function that raises a
  descriptive error where that's available. A blanket ignore, an
  `assert x is not None` added to quiet a checker, or a cast asserting what you
  wish were true are all papering over.
- In table-driven tests, suppress a rule that fights the canonical table
  layout, such as a statement-count limit on a long table, because the rule
  doesn't understand the pattern.

## Errors and Inputs

`assert` is for programmer errors. A schema can permit something your code
doesn't handle, and turning that into an `AssertionError` traceback gives the
caller nothing to act on. Raise something descriptive instead.

Don't mutate inputs. Return a populated copy if you need defaults applied, so a
later reader can still tell what the caller actually sent.

## Docstrings

Give each function and helper a docstring. The reader can already read the
code, so make it short. One that grew a paragraph per round of review wants
rewriting rather than extending, and fifty lines to explain eight environment
variables is a defect rather than thoroughness.

## Tests

`engineering-principles` covers the principles. In Python:

- **Follow the Tests section of CONTRIBUTING.md.** It's the reference: plain
  pytest functions over a table of `Case`s, with no classes or fixtures.
- **Compare at the highest level the test allows**, in one assertion over the
  whole response rather than several over slices of it.
- **Build the XR from its model, and write resources as dicts.** The generated
  models fill in schema defaults the function doesn't emit, so a composed or
  observed resource built from one won't match the real response.
- **Start without helpers** and add one only when it cuts real noise. Helpers
  that build an individual resource are fine. Helpers that build whole cases are
  not, and neither is deriving one case from another by copying and mutating it.

## Key Principles

1. Legibility beats DRY and efficiency. One pattern that scales up and down
2. Use the typed model everywhere one exists, reads and tests included
3. Never hand-edit generated code. Fix the generator and regenerate
4. Boring Python. No cleverness a reader has to decode
5. Don't alias expressions that already read well
6. Guard clauses over compound conditions and nesting
7. Name modules for what they hold
8. Import modules, not names
9. Annotate declarations, and only with what a checker verifies
10. A type error reports a design problem before it reports a missing ignore
11. `assert` is for programmer errors, not for input the schema permits
12. Docstrings are short. Tests are tables of models
