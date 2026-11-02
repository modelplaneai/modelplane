# Calibrating Design Questions

These are real design concerns from Modelplane PRs, and what came of them. Use
them to judge which questions change a design, and how to ask them.

## Changed the design

On #362, the ModelCache design:

<!-- vale off -->
> What is the purpose of ModelCache? Is it: 1. Get weights on a cluster before
> any model replica runs there 2. Get weights on a cluster the first time a
> model replica runs there 3. A bit of both? ... It was something like 45
> minutes to hydrate the cache PVC, then another 15+ minutes to load from the
> cache PVC.
<!-- vale on -->

The design was rebuilt around staging weights in advance. The reviewer asked
what the user would experience, with the options laid out and numbers on the
wait.

Later on the same PR, a revision began injecting `--model` into the engine:

<!-- vale off -->
> Doesn't this go against our overarching design principle of not injecting
> engine args though? What if a new engine shipped tomorrow that used a
> different flag for this?
<!-- vale on -->

A fix for one problem broke a principle the committed design states. Check
each revision against those principles, not only the first draft.

On #514, the prefill/decode routing design:

<!-- vale off -->
> This seems like a case where we'd need to ship a new Modelplane to support
> new engine features - e.g. new types of KV transfer. ... I don't think
> there's a principled reason for MD to be how it is today: with all engine
> configuration exposed to the user but (almost) all routing configuration
> abstracted and/or opinionated.
<!-- vale on -->

That question led to four revisions of the API. Testing a design against an
engine or router it doesn't name yet is the most productive question in
Modelplane's design history.

On #112, an API with one source of models so far:

<!-- vale off -->
> I think we should add a required discriminator field now, even though we
> only support HF. ... I prefer required discriminator fields because it forces
> the user to be explicit about what they want, and makes the code that handles
> the API simpler.
<!-- vale on -->

The field went in. A required field can't arrive later without breaking users,
so add it before the second variant exists.

On #363, the metrics design, a question that followed the data to its reader:

<!-- vale off -->
> I think we need to step back and think about what we want to help folks
> monitor.
<!-- vale on -->

The review asked who reads each kind of metric and how, and the design moved
its on/off switch to where the people who consume the metrics configure them.

## Raised, and the proposal held

On #116, a design for prefill/decode deployments:

<!-- vale off -->
> Another alternative that comes to mind: could you just write two MDs? One for
> prefill and the other for decode? It feels really close to what's proposed,
> and I like that it just reuses existing primitives. However I think what's
> proposed is better
<!-- vale on -->

The reviewer raised the reuse-what-exists alternative and argued it fairly,
then concluded the proposal was better. It went into the doc's alternatives.
Asking was right even though the answer was no.

## Declined

On #64, reserving enum values for APIs Modelplane might support one day:

<!-- vale off -->
> Closing — extension path can be designed when a real second API lands.
<!-- vale on -->

Future-proof where adding something later would break users. Don't enumerate
imagined futures.

On #542, a request for a heartbeat in case the cluster agent froze:

<!-- vale off -->
> A frozen agent makes pools look fuller, not emptier. The scheduler keeps
> charging every new replica, because it can't count one as observed until the
> agent recovers.
<!-- vale on -->

The failure already erred in the safe direction. Ask which way a failure errs
before proposing a mechanism to prevent it.

On #422, a request for per-component overrides of the serving stack's
defaults:

<!-- vale off -->
> Values like PDBs and security contexts: if our defaults are missing a strict
> non-root context or a PDB, that is something we should fix in our/aicrs
> defaults, not a missing override. Otherwise every team fixes the same thing
<!-- vale on -->

An escape hatch can paper over a default that should change. Ask whether the
default is what's wrong.

## What to take from this

- The questions that changed designs asked what the user gets, or whether the
  API would need a new release for the next engine. Ask those first.
- Ask about an alternative even when you expect the proposal to hold, as long
  as you argue the alternative fairly.
- A concern about a hypothetical future, or a failure that's already safe,
  rarely changes anything.
- Some concerns need context no agent has, such as a decision made in Slack or
  where a release's scope ends. Where a design seems to rest on something you
  can't see, ask about it rather than assuming it's missing.
