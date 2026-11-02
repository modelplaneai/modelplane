---
name: reviewing-designs
description: >-
  Judge whether a Modelplane design or approach is a good one, from whether
  its problem is real to whether its API will hold up. Use when reviewing a
  design doc, when checking a PR's premise against its issue and the committed
  designs, or when weighing alternatives. For the quality of a design doc's
  writing, use design-docs.
---

# Reviewing Designs

This covers whether a proposal is good, not whether it's well written.

Maintainers own design decisions. As a reviewer, ask the questions a
maintainer would ask and leave the answers to them, since you're better at
making sure a question gets asked than at deciding it.

## What's Settled and What Isn't

Treat a design doc committed on the base branch as settled. Review a PR against
it, and don't argue with it. Where a PR departs from it, ask whether the
departure is deliberate. Don't ask whether the committed design was right.

A design doc the PR adds or changes is under review, so review it. A design
records direction, mostly API and UX. It doesn't have to match the code
exactly. A design that actively misleads about how the system works is a
finding.

Reopening a settled decision is sometimes right, when the PR shows the decision
doesn't hold. Say so plainly: "this reopens X from the design, because Y".

## The Questions

These are the questions maintainers ask of a proposal, roughly in the order
they matter.

### What does the user get?

- What problem does this solve, and for whom? How does it help them compared
  with today?
- Who consumes each new field, status or resource, and how? Follow every
  artifact to whoever creates it and whoever reads it.
- Could a user do this today without the change, with the right image, flags
  or existing resources?
- Does it matter in practice, or is it an optimization nobody would notice?
  Ask for numbers where the case rests on them, such as how long a user waits.

<!-- vale off -->
> What is the purpose of ModelCache? With pre-staging, the 45 minutes is paid in
> advance, so the user experience for an MD author is "my model is ready in ~15
> mins".
<!-- vale on -->

### Does each piece need to exist?

- Could this layer, field, resource or code path go? What would a smaller
  version lose?
- Is there an existing primitive that already does this, such as a
  Composition, a second ModelDeployment, or `podSpecTemplate`? Raise the
  reuse-what-exists alternative even if you expect it to lose.
- Does a new concept duplicate one Modelplane already has?
- Is an override or escape hatch covering for a default that the PR should fix
  instead?

<!-- vale off -->
> Should we just use an EPP all the time? It'd be vestigial for the single pod
> case, but presumably simplify the system.
<!-- vale on -->

### Will the API hold up?

The krm-api-design skill has the rules. The questions that matter most:

- Would a new engine, engine version, router, cloud or accelerator need a new
  Modelplane release? Test the API against a second one, such as SGLang beside
  vLLM, or Dynamo beside llm-d.
- Does it encode one engine's or one cloud's specifics into a generic API? Does
  it leak an implementation detail, such as LWS, InferencePool or the EPP's
  config, that users then depend on?
- Does it encode today's topologies into the schema, rather than letting users
  describe their own?
- Where there might be alternatives, is there a required discriminator and a
  subobject per variant? Adding a required field later breaks every user.
- Does it future-proof only where adding something later would break users? A
  list of enum values reserved for imagined futures is speculative.
- Is upstream configuration that changes often, such as engine flags, left
  opaque? Is a stable upstream API, such as a pod template, mirrored as a
  compatible subset?

> Would a new vendor need a Modelplane release to support this? It looks like
> the routing block encodes vLLM's P/D flags.

### Does it fit?

- Does it hold to the principles in the committed designs? Re-check each
  revision against them, since a fix for one problem can break a principle,
  such as "Modelplane doesn't inject engine flags".
- Does it treat like things alike? If users set one option directly while
  Modelplane picks a sibling option for them, what's the reason?
- Who configures each part, the platform team or the model's author? Is the
  split principled?
- Does it use upstream projects the way their authors intend, or invent its own
  interpretation? How much is upstream and how much is ours?
- What does it ask of people who extend Modelplane, such as a composition
  author for a new cloud?
- Does it overlap an open PR or issue?
- Does a user need to understand Crossplane to use it? Needing it installed is
  fine. Needing to understand it isn't.

### Does it work?

- Has the author shown someone could build it? Are there gaps in how it would
  work?
- What happens at the edges? A user omits a field, hardware autoscales, a
  dependency fails permanently, or requests are in flight during a change.
- When it fails, which way does it fail? A default is fine if getting it wrong
  costs a little quality. It isn't if it produces something that reports Ready
  and rejects requests.
- Does it assume something of clients that real clients don't do, such as
  sending a special header?
- Does it make trying Modelplane out harder, such as needing a real domain or a
  TLS certificate before the first request?
- Does it judge by today's scale, or by the ceiling it needs to reach? Judge by
  the ceiling.

### Is anything hard deferred?

Deferring is fine when the design could add the deferred part later without a
breaking change. It isn't fine when the deferred part determines the API's
fields or structure. Phrases like "for now we can just" and "we can add it
later" signal a deferral to check. Ask whether the API would still fit once
someone builds the deferred part properly.

## Alternatives

- Is each rejected alternative given its strongest form? What would make it
  work?
- Is the reason it lost the real one? If the real reason is governance or
  ecosystem, the doc should say so rather than give a technical one.
- Is there an obvious alternative missing, such as reusing what exists, or what
  a comparable project does?

## What Not to Raise

- Breaking changes and migration. Modelplane is pre-1.0. A breaking change that
  makes the API better is welcome.
- Settled decisions in committed designs, or ones a thread already settled.
- Code-level detail in a design doc. Designs cover concepts, APIs and UX.
- Requests to make a design match its implementation exactly.
- Omissions the doc or the PR marks as out of scope or deferred, where adding
  them later wouldn't break anything.
- Commercial or strategic motives. Public designs argue technical merit.
- Detail that wouldn't change the design, such as a timeout value.

## How to Raise It

Phrase design questions as reviewing-pull-requests describes: ask, explain,
suggest. Where it helps, sketch an alternative in YAML and say you haven't
thought it through: "please poke holes in this rather than taking it as-is".

Getting a fact wrong costs the whole review its credibility. Check a claim
about how Crossplane, Kubernetes or an upstream project behaves against its
source before you ask a question that rests on it.

[references/calibration.md](references/calibration.md) has real design concerns
from Modelplane's history and what came of them.
