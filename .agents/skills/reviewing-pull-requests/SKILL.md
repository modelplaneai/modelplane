---
name: reviewing-pull-requests
description: >-
  What to raise when reviewing a Modelplane pull request, and how to phrase
  it. Use when reviewing someone else's PR, when
  drafting review comments or a review summary, or when triaging findings from
  a review agent or bot.
---

# Reviewing Pull Requests

This covers what Modelplane's maintainers look for in a PR and how they phrase
it.

CONTRIBUTING.md wins wherever it disagrees with this skill. Don't spend
comments on anything CI already enforces: ruff, ty, Vale on the docs site, the
function test-style checker, the list-selector checker and manifest validation.

## The Bar for Posting

A reviewer told to find problems will always find some. Agent reviewers find
them indefinitely, and with the author's agent addressing each one, a low bar
turns review into churn. Post a finding only if all of these hold:

- **It's true, and you verified it.** You traced the code path or ran the
  test that shows it. A claim about how Crossplane, Kubernetes, a
  provider or any other tool behaves needs a link to the source or docs that
  shows it. If you can't verify it, don't post it.
- **This PR introduced it.** A pre-existing problem is not this PR's finding,
  unless the PR spreads it further.
- **It can happen.** A failure needs a concrete input or sequence that causes
  it, in a deployment someone would run. A state the schema, the type or an
  admission rule already rules out is not a finding.
- **The author would want to change it**, knowing everything you know.
  Interesting is not the same as important.

Don't raise these at all:

- **Backward compatibility, migration or upgrade paths.** Modelplane is
  pre-1.0 and nobody needs migrating. Challenge compat shims and migration docs
  instead.
- **Guards for states that can't happen.** A runtime check for something the
  XRD schema or a CEL rule enforces puts one invariant in two places.
- **A second enforcement point.** If an invariant could be a CEL rule, ask for
  the CEL rule rather than a check in the function.
- **Requests to do more.** Don't ask a PR to add features or take a different
  architecture than it sets out to. Design questions about what it does are
  the design reviewer's, and the reviewing-designs skill covers them.
- **Settled questions.** Read the existing review threads first. Don't re-raise
  something already discussed, resolved, or declined.
- **Missing E2E coverage.** E2E runs in a separate, label-gated workflow.
- **Performance micro-optimisations** without a measured problem.
- **Documentation of ordinary language semantics**, such as who owns a map a
  caller passes in.

[references/calibration.md](references/calibration.md) has real findings and
what happened to them. Read it before deciding what to post.

## How to Phrase Feedback

**Frame correctness and design feedback as a question.** That covers bugs,
architecture, API shape, and docs or comments that say something untrue. Use
three parts: ask, explain, suggest.

1. Ask about the problem you think you see, not about your fix.
2. Explain why you think so, hedged as what it looks like from here: "It looks
   like...", "I think...".
3. Suggest an alternative, also as a question, when one is clear: "What if
   we...?", "Could we...?". Where two or three fixes come to mind, offer them
   all, and say when you don't mind which. Don't invent options to have more
   than one. A bare question is fine when no fix is clear.

You may be missing context, and a question invites the author to supply it.
Authors also feed review comments to their own agents, which take a comment
stated as fact at its word and act on it even when your analysis is wrong.
Asking makes them check. Ask even when you're confident.

Offering options matters for the same reason. An agent handed one fix applies
it as written. Handed a few, or told the reviewer doesn't mind how the problem
gets solved, it has to weigh them, and may find a better fix than any of yours.

These are real comments that follow the pattern. Phrase your own for the PR in
front of you rather than reusing their wording.

> Won't `ready != len(refs)` always be true if `renderFail` is true? Or are
> there cases where we need both?

> Does using kwargs here weaken the type checking we'd get from using
> `ModelReplica`? Could we instead form the ModelReplica model outside of
> `resource.update` and set `replica.spec.modelCacheRef` only if the
> ModelDeployment has one?

> Do we need to hold composition until the gateway is Ready? With the anchor
> in place, I'd expect a route created during a rollout to be harmless. If so,
> always composing and gating only `RoutingReady` would let us drop this
> special case.

> Won't this leave a stored prefill/decode replica's `ModelAccepted` and
> `ModelReady` as `Unknown`? On a fatal result Crossplane keeps the conditions
> a function sets, and marks every custom condition it didn't set as
> `Unknown`
> ([reconciler.go](https://github.com/crossplane/crossplane/blob/v2.4.0/internal/controller/apiextensions/composite/reconciler.go#L792-L803)).
> So a pair that's still serving would show `ModelReady=Unknown`.

A design question especially needs the hedge, because the author has context
you don't. State your assumption and invite correction ("I assume the main cost
here is X, right?"). Give the cost of your suggestion, not only its benefit, and
say whether the gap is in the design or only in how the doc explains it.

**Be direct about style, readability and maintainability.** "Please use the
established pattern here, because..." is fine, as long as the comment gives the
reason. Where it helps, link to CONTRIBUTING, a skill or an existing example in
the repo. The author should come away knowing the convention, not only with a
fixed line. Mark minor points with `Nit:`.

> Nit: "Gets deployments" sounds like this will return deployments, when it
> actually seems to apply the supplied ModifyFn.

> This is documenting a change. Document how it works, not how it differs
> from how it used to work. (Here and elsewhere.)

Write short comments. Most run two to four sentences, ending with the
suggestion.
Make one point per comment. If the same issue
recurs, comment once and add "(here and elsewhere)" rather than repeating the
comment.

A comment that proposes an alternative may also sketch it, as a short YAML or
code block with a sentence or two on what it buys. Show only the parts
that differ, not a full implementation. Proposing an alternative is the only
reason a comment runs longer. Importance isn't one. Keep a comment's prose,
outside any code block, under about 600 characters. Get there by cutting
words, never by dropping a finding.

Don't prescribe a fix in detail. Sketch it, or link to an example, and let the
author work it out. Authors feed comments to their own agents, and a
prescriptive comment gets applied verbatim whether or not it's right.

Don't use GitHub suggestion blocks. Applying one creates a commit without a
`Signed-off-by` line, which fails DCO.

Write plainly, as a maintainer would. The writing-style skill applies to review
comments as much as to docs.

## Where Comments Go

Attach every comment to a line the PR changed, in the most relevant place:

- A finding about a line the PR changed goes on that line.
- A finding about code or docs the PR didn't change goes on the change that
  causes it. If a comment or docs page elsewhere is now untrue, comment on the
  change that made it untrue, and say which comment or link the page.
- A design question goes on the field, design doc section or code it's about.

## The Review Summary

The summary opens with one hedged sentence on how close the PR looks to
mergeable, without repeating any inline comment. It places the PR on a scale
from ready, through close with a few questions and needing another look at
specific things, to needing a step back on the approach. Say it in your own
words for this PR, naming what stands between it and merging where that's one
thing. The design reviewer's level weighs most in choosing it. Don't grade the
PR beyond that: leave out verdicts like "well tested" or "reads well".

The rest of the summary explains that verdict in two or three sentences, and
the whole summary stays under 1,000 characters. It says what the verdict rests
on without restating the inline comments about it, which the author reads
next. Every finding goes inline, as the previous section describes.

A finding about the PR as a whole, such as whether to split it, belongs in the
summary when it's the reason for the verdict. Findings about the commit messages
or PR description get one closing sentence there. Don't describe what the PR
does. Maintainers ask their own agents when they want that, and the PR
description is the author's job.

Never approve or request changes. That's a maintainer's call.
