---
name: engineering-principles
description: >-
  Language-agnostic engineering principles, and the trunk the language skills
  branch from. Load it alongside python-code-factoring whenever you write,
  refactor, restructure, fix or review code. Covers how to approach a change
  in an existing codebase, how much weight an existing pattern deserves,
  writing less code, the standards a test has to meet, and what belongs in a
  comment.
---

# Engineering Principles

These principles hold in any language, and cover how to approach the work rather
than its syntax.

## How to Approach a Change

Read the code the change touches, and the code around it. Then work in this
order:

1. Work out what the change actually is.
2. Design the ideal implementation. Not the smallest diff: the code you would
   write greenfield, knowing what you now know. What's already there is an input
   to this step, not the answer to it.
3. Price the gap between that and what exists. Is it a breaking change or a
   large refactor? Does it risk regressions somewhere you can't easily test?
   Weigh that against a strong desire to leave the codebase simpler, more
   modular and easier to extend than you found it.
4. If the full move really is too risky, do the largest part of it that still
   leaves the codebase better than you found it. Not the smallest change that
   works.

Write like a senior engineer with the standing to improve the codebase, and hold
yourself to a higher bar than an average one trying to land their ticket.

Expect the user to ask whether the fix really is as expensive as you claimed, so
check before saying it is. "We can do that in a follow-up" is usually laziness
rather than sequencing: if your change is what introduces the problem, fix it
here rather than filing an issue about it.

The exception is work that's speculative rather than deferred. Park a refactor
grown out of proportion to a problem nobody has hit yet, and say so.

### Consistency has a price, and it varies

Existing patterns get their weight in step 3. Deviating from one fragments
something other people have to keep reading, so it costs something. How much
depends on where the pattern came from.

- **Designed and reviewed by people, over time.** Breaking consistency with it
  is expensive. Match it, and if you think it's wrong, say so rather than
  quietly deviating.
- **Written by LLMs with light human steering.** The pattern may be one
  unreviewed choice nobody vetted, so matching it propagates it. Weigh it
  against the greenfield design on merit.

When you can't tell, find out when the pattern arrived and who put it there,
then ask rather than guessing:

> "If you're unsure whether to follow pattern or something potentially more
> correct, ask me."

Introducing a new pattern has its own cost, so weigh "more correct" against
having two ways to do one thing.

Being in the codebase is not the same as being current. A codebase often still
contains a pattern it is migrating away from, so check that what you're matching
is what it is moving toward.

## Write Less Code

The most common defect in agent-written code is code that didn't need to exist.
It arrives as helpers, wrappers, layers and guards, each individually
defensible, and it accumulates into something nobody can read.

**Inline by default.** Something broken out and then called once is
indirection rather than abstraction, and a long parameter list on a private
method is the tell. Extract when the logic is complex enough to need a name, or
when a test needs an injection point. Check for prior art first: a helper that
duplicates one two modules over is worse than the repetition it removes.

**Prefer repetition to indirection.** Aim for the dull middle: code that is
neither duplicated everywhere nor abstracted into something clever. Given a
choice between repeating four lines and introducing a type, repeat the lines.

**Don't guard states that can't happen.** Trust a field that a schema, type or
admission check has already made required. Nil checks, `or` fallbacks,
truthiness guards and broad exception handling for impossible inputs add noise,
and when they do eventually fire they silently swallow real data. Push the
invariant out to the boundary that can enforce it, then rely on it.

**Delete what a change makes redundant.** When you fix a root cause, remove the
workaround in the same change. When you replace a mechanism, remove what it
replaced. When a field, parameter or branch loses its last consumer, remove it.
Production code whose only callers are tests is dead code.

**Don't paper over.** A suppression, an ignore comment, a hardcoded status, a
weakened schema, or an assertion adjusted to match the output are all ways of
deleting a signal rather than fixing what it reports. If a check is fighting
you, work out whether it's right before suppressing it. Suppress with a stated
reason only when the check is wrong about your code.

**Fix the class, not the instance.** Once you know about a defect, look for the
rest of it before calling it fixed. The same bug usually sits in the sibling
code path, and the same smell elsewhere in your own diff. This holds whether you
found it or someone pointed it out.

## Design for Change

This applies to contracts, not to code structure. A schema, an API, a wire
format, or the signature of a function other people call are all expensive to
change once something depends on them, so choose shapes that evolve additively.
A field that might plausibly need more than one value wants to be a list, and a
boolean that might become three options wants to be an enum. Where you're
mirroring an upstream type, stay a strict subset of it so you can grow into it
later.

It does not apply to code you're inventing. A function that might one day have a
second caller is speculative, and so is an interface with only one
implementation. Add them when something needs them.

Ask what stage the project is at before paying to *avoid* a breaking change.
Where nothing is running the software yet, migration shims and deprecation
cycles buy nothing, so do whatever is most correct.

That is not licence to design the contract carelessly. Aim for a contract that
never needs a new version, whether or not you could get away with breaking it
today. Being free to change something is a poor substitute for not needing to.
Where a restriction is easy to lift later, releasing it and waiting for someone
to complain still beats guessing.

## Don't Defer the Hard Things

In new work, the hard parts usually hold the risk you most want to retire early:
whether an idea is feasible, and whether the interface and the approach hold up.
Deferring them hides that risk until discovery costs most.

Simplify the approach freely, with a naive algorithm, an in-memory store or a
hardcoded value. Don't simplify away the problem itself. Removing work is fine,
removing the question the work has to answer is not.

Order the work by risk rather than by ease. Start with feasibility, then the API
design and data model, and then the choices that are expensive to reverse.
Integration points come next, and features come last. Everything above features
is a contract something else will depend on.

These phrases are the tell that someone is deferring something hard because it's
hard: "for now we can just", "we can add it later", "to keep v0.1 simple", "we
don't need to worry about that yet". Sometimes the deferred thing really is
peripheral, so treat them as a prompt to check rather than a verdict. But if it
affects API shape, data model, or architecture, later means rewriting.

None of this applies to a bug fix or an incremental change in a settled
codebase. There the risk is already retired, and the job is to fit in.

## Verify, Don't Assume

Confidence is not evidence. Before using an unfamiliar API, read its actual
documentation rather than recalling it from memory, because training data is
full of outdated signatures and plausible-but-wrong patterns. Before declaring a
change correct, run the tests, run the build, and read the diff with fresh eyes.

## Comment the Why, Not the What

The code already says what it does, so a comment that restates it is noise.
Comment what the code *can't* say, such as the reason for a non-obvious
decision. Above all, comment the external constraint behind it, such as an
upstream library's behaviour, a protocol quirk, an API that rejects the obvious
approach, or a bug you're working around. That context is expensive to
rediscover.

```go
// SetConditions sets conditions to nil if passed no arguments. SSA interprets
// this as null and rejects it, so we only set them when there's something to set.
if len(cs) > 0 {
    xr.SetConditions(cs...)
}
```

If a reader could derive the comment from the line below it, delete the comment.
If they'd need another package's source, a changelog, or an issue to understand
why the code looks this way, write it down.

### Describe the State, Not the Change

When you change code from approach X to approach Y, don't leave a comment saying
"do Y, not X". The reader has Y in front of them, and wants to understand why
the code is the way it is *now*. `Use a map here, not a slice` or
`Switched to SSA instead of patching` documents your edit, which belongs in the
commit message rather than the source.

```go
// Bad — narrates the change.
// Use errors.Is here instead of == so wrapped errors still match.

// Good — describes the current state and its reason.
// The client wraps this error, so compare with errors.Is rather than ==.
```

Mention the old approach only where a reader would reasonably wonder "why not
the obvious way?", or might want to refactor back to something that was bad for
a non-obvious reason. There it's part of the current code's rationale rather
than a history lesson.

Mark deferred work with `TODO(user):`, and a decision that looks wrong until you
know the reason with `NOTE(user):`, so the next reader knows who to ask and that
it was deliberate. Phrase a TODO as the open question it is ("Use server-side
apply instead?"), not a vague "fix this later".

## Structure for Testability

Testability is a design property, decided when you structure the code. Code
that's hard to test is usually telling you the design is wrong: the code
hardwires dependencies it should accept as parameters, and mixes side effects
with logic in a function that does too much.

Build in the injection points as you write. Being unable to test something
without standing up real infrastructure means you should restructure the code
rather than skip the test.

### Test standards

The language skills cover the local mechanics.

- **Assert the whole output.** Compare the entire returned value against an
  expected one. Picking out a few fields passes silently over the fields you
  didn't think to check, which is where the bug is.
- **Cases are data.** Table-driven, with each case written out in full.
  Repetition between cases is fine and factoring case construction into a helper
  is not, because the helper hides what each case asserts. Deriving one case
  from another by copying and mutating it hides it twice over.
- **A standalone test that runs the same path with different data is a case.**
- **Never compute the expected value with the code under test.** An expectation
  produced by the production function passes under any implementation.
- **Fixtures for generated or third-party shapes are real artifacts.** Copy a
  real one rather than hand-writing your approximation, and cover every variant
  the code has to handle. A hand-written fixture tests your mental model.
- **Prove it can fail.** Break the behaviour, or revert the change, and watch it
  go red. This matters most for concurrency, and for anything where a green run
  might mean the fixture never built the condition or the test never executed
  the path.
- **A test that only asserts the output hasn't changed is a change detector.**
  It breaks on every legitimate edit and protects nothing. Assert a property
  someone depends on, or don't write it.

Follow the pattern already in the file. Where the change will merge with another
branch, follow that branch's pattern rather than the one on the main branch.

## Errors Belong to Whoever Broke the Contract

Work out whose invariant a failure violates before deciding how to handle it. A
malformed request from a caller is that caller's bug, and quietly compensating
for it hides the bug in both places. Report it rather than handling it silently.

## Key Principles

1. Design the ideal implementation first, then price the gap to what exists
2. Leave the codebase better than you found it, not minimally disturbed
3. Weigh an existing pattern by where it came from. Ask when you can't tell
4. Write less code. Inline by default, repeat rather than abstract
5. Don't guard states the schema already prevents
6. Delete what your change makes redundant, in the same change
7. Don't paper over a signal. Fix what it reports
8. Fix the class of defect, not the instance someone showed you
9. Design for change in contracts. Not in code you're inventing
10. Don't defer the hard things. Simplify the approach, never the problem
11. Verify, don't assume
12. Comment the why, not the what
13. Structure for testability. Seams are a design decision
14. Assert whole outputs against real fixtures, and prove the test can fail
15. Handle an error where its contract broke, not where it surfaced
