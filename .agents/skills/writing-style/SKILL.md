---
name: writing-style
description: Write and review prose in Modelplane's style. Use when drafting or editing designs, docs, code comments, field descriptions, commit messages, PR descriptions or review comments. Also use when reviewing the prose in someone else's change, or when tightening, de-slopping, or copy editing any prose.
---

# Writing Style

Modelplane's prose follows the style of Nic Cope, its lead maintainer. The
maintainers read every word and delete whatever the reader doesn't need, so aim
for a draft they'd leave as it is. The quotes below are Nic's corrections to
agents, kept as evidence for each rule.

Most of this skill covers what goes wrong. An LLM's instinct for a good design
doc is roughly right about voice and section structure, and badly wrong about
length, honesty about rationale, and who the document addresses. So the failure
modes come first.

For voice, the section-by-section skeleton, and worked examples of the target,
read [references/voice.md](references/voice.md). Read it when drafting from
scratch. Skip it when tightening prose that already exists.

## When to Use This (vs Other Skills)

Use **this skill** for how prose reads, wherever it appears: designs, docs, code
comments, XRD field descriptions, commit messages, PR descriptions and review
comments. CONTRIBUTING.md sets the conventions specific to commits, PRs and
issues, and "Cut Hard, Twice", "Never Invent Rationale", and "Substantiate or
Delete" below apply to them unchanged.

Use **design-docs** for the structure of a design doc.

Use **reviewing-pull-requests** for what to raise in a review, and this skill
for how to word it.

## Reviewing Someone Else's Prose

The failure modes below are also the checklist for reviewing prose in a PR.
Raise the defects a reader would trip on, such as a claim the code doesn't
support or invented rationale. A comment three times longer than it needs to be
is another. Quote the text, say what's wrong, and leave the rewrite to the
author.

CI already runs Vale over `docs/`, so don't repeat what it reports there.
Elsewhere, such as design docs and code comments, raise a pattern of AI-written
phrasing in one comment.

## Before You Draft

Establish three things, and ask rather than guess:

1. **Audience.** Who reads this? Maintainers, contributors, users, a partner's
   engineers? "High level but deeply technical" produces a different document
   from "internal, to align on asks".
2. **Purpose.** What decision does it serve, or what does the reader do
   differently afterwards?
3. **Premise and scope.** What does this document assume, and what falls outside
   it? State the premise in the document rather than hedging across several
   scenarios.

Almost every framing correction traces back to a document drafted without a
specific reader in mind. Naming the reader up front prevents most of them.

**Public documents argue technical merit alone.** Where the real motivation is
commercial, find the honest technical reason or don't write it. Never dress a
commercial motive as engineering rationale, and never write a circular one.
"We're adding support for this engine because we said we'd add it" is not a
background section. If you can't find a real reason, say so and ask.

## The Failure Modes

### Cut Hard, Twice

Your first draft runs about 40% longer than it should. That's predictable enough
to treat as a step in the process. Draft, cut 30%, then read it again and remove
the 10-20% still there. Nic's calibration, verbatim:

<!-- vale off -->
> Even when I ask you to cut 30% of what you write I can easily find another
> 10-20% that can be cut with zero loss.
<!-- vale on -->

Do both passes before anyone reads it. A draft the reader has to cut costs them
the time the document should have saved, and Nic has said plainly that
repairing LLM prose is expensive:

> Generally when you write a document it costs me a lot of time spent getting it
> to an actually good state.

Delete first:

- Sentences restating the previous sentence in different words.
- Throat-clearing and hedging preambles.
- Anything a competent author would obviously have done.
- Explanation the reader can reconstruct from what you already wrote, or from a
  link. Link out instead of paraphrasing.
- Trailing summaries, recaps, and closing questions. An assessment is a
  statement.
- Background the argument doesn't rest on. Explain only the property the
  argument needs: that Volcano and Kueue can gang schedule LeaderWorkerSet,
  without explaining how either works.

Accuracy is never a reason to grow. When correcting a claim, keep the fix inside
the existing footprint. "Give me a more accurate version of my last paragraph
that isn't any longer" is a routine request, as is a copy edit capped at one or
two extra lines.

Prose is the expensive part of a document. Trading paragraphs for a worked YAML
example is usually a win. After a concrete sketch, delete the prose that repeats
it and keep only what the sketch can't express.

Depth of thinking and length of output vary independently. "Think hard, but
answer succinctly" is a coherent instruction.

### Write for the Reader, Not Against the Conversation

Nic makes this correction more sharply than any other, and you won't infer it,
because from inside the session the reactive version feels responsive.

<!-- vale off -->
> You're once again doing the thing where: A) you write a doc, B) I criticize a
> point, C) you edit the doc to specifically react to that point, explicitly
> naming the point (without reason) throughout. For example your doc now
> litigates what requirements are due to fleet and what aren't, when the reader
> doesn't actually care.

> This is garbage writing. You're writing this section in reaction to the
> discussion we just had. Think about the audience. Think about the flow of the
> document. Rewrite the section from first principles.
<!-- vale on -->

It appears two ways. The document argues with a reviewer's objection, or it
argues with its own earlier draft. Phrasings like "this replaces an earlier
approach", "GPUs per node isn't a…", and "not a fleet layer on top of a cluster
level platform" all give it away.

To catch it, check whether a sentence would make sense to somebody who never
read the review thread or the previous draft. Delete it if it wouldn't. The
reader has access to neither.

Feedback changes what a section says, and never makes the section defend itself.
So rewrite from first principles, positively, as though nobody had raised the
objection. Patching won't do. A framing error in one sentence usually means the
section needs redoing, and patching prose in place is how documents become
incoherent.

Where a reader might reasonably ask "why not do it the other way", answer that
in Alternatives Considered. An inline rebuttal is the wrong home for it.

### Never Invent Rationale

The reason behind a decision comes from the author's intent. A diff shows a
rename without saying why. A plausible reason invented to fill the gap is worse
than no reason, because the reader can't tell it apart from a real one and it
becomes part of the permanent record.

If you don't know why, ask. Nic has said so outright:

> DO NOT invent justification unless you know for sure why something is like it
> is. You can ask me if you don't know.

When he catches it the response is blunt: "You just made that all up."

This extends past rationale:

- **Invented terminology.** Don't coin a phrase that sounds like a term of art
  and isn't. Check a term has currency in the domain before asserting it does.
- **Invented APIs.** Never show a field, resource, or flag that doesn't exist,
  above all in another project's API.
- **Invented policy.** Don't document a rule that nothing enforces.
- **Invented examples.** See "Examples Are Real" below.

An Alternatives Considered entry records what actually happened, and each
argument attaches to the alternative it truly applies to.

### Substantiate or Delete

Assertion is not argument. "Dynamo's KV cache offloading is ahead of vLLM" needs
the mechanism and the baseline: ahead how, and what does vLLM do? Expect the
question "why?" about any sentence claiming a causal necessity, and "what's your
source for that insight?" about any confident claim. A sentence you can't defend
is filler, so remove it.

Root claims in primary evidence. For behaviour the code decides, over the docs
and over the README.

Word choice must not imply a conclusion the facts don't support. Describing a
cache as LRU to suggest it's worse, when it also supports frecency, is a writing
defect as much as a factual one. Evaluative adjectives need a definition and
even application. Don't call one project's feature "experimental and niche"
without assessing its peers the same way.

Avoid a partisan frame. A comparison of two stacks is not a contest between us
and them, and rhetorical absolutes rarely survive scrutiny.

### Space Signals Importance

The room a topic gets is how a reader judges its importance, so proportion
is a property to check. Nic checks it by eye and by line count: "Your new
section takes up more space than any other in the doc. Should it?"

- Sections stay proportional to their importance to the whole document, never to
  how recently they came up. Recency bias is a defect. A commit message follows
  the diff, not the order the work happened in.
- List entries stay proportional to their siblings. Any number of entries is
  fine. One entry three times the size of the others because it's your current
  focus is not.
- After trimming one section, trim its neighbours. Cutting one part leaves an
  adjacent part oversized.
- Real content doesn't belong in parentheses. Anything that merits a mention
  merits a sentence, and something that deserves billing equal to its peers
  deserves a paragraph.
- One concept per list entry. Don't bundle rate limiting and authentication
  under "identity", and don't drop an item while reorganising.

### Write the Design as If It Preceded the Code

A design records direction. It concentrates on APIs and UX with broad-strokes
mechanics. It doesn't mirror the code, and it isn't a recipe to build it. It
must not actively mislead, though it may show things not yet built.

So write in the present tense, describing the current position as though it were
the first and only one:

- No changelog framing, since the reader doesn't care how a previous
  unreleased iteration worked.
- Discarded thinking belongs in Alternatives Considered, gathered there rather
  than scattered inline.
- No links to issues or PRs in the same repo. They expose the document's real
  chronology, and a design should read as the first artifact in the repo. Link
  generously outward to upstream issues, specs, talks, and prior art.
- A superseded document isn't rewritten. It gets a one or two sentence preface
  saying it remains for historical context.
- Don't link a durable document to a scratch file you'll delete.

At some point a design stops absorbing revisions and gets extended by
supplementary designs. Prefer minimal surgery to a rewrite, because it's cheaper
to review.

A proposal proposes the intended end state, and leaves out the limitations
currently blocking it. Goals appear positively, as what gets delivered, and at
project level rather than scoped to a release. Don't confess open unknowns in
passing. Stay deliberately vague where the mechanism remains unsettled, and
state the requirement instead.

Leave out an Open Questions section. A design takes a position. Opinions belong
inline beside the thing they concern, rather than in a trailing "positions" or
"recommendations" section.

### Examples Are Real

An example does more work than the prose around it, so give it the same
scrutiny.

- **Reuse validated artifacts from the repo.** Copy the tested manifest instead
  of composing one from memory, and use the same example throughout the
  document, including the executive summary.
- **Show whole objects.** A full ModelDeployment, and not a `spec:` fragment.
- **If you can't verify it, say so.** "I couldn't build a credible example here"
  is better than a plausible fabrication.
- **Calibrate realistically.** Selectors should ask for what a model actually
  needs, and examples should reflect the scale users really run.
- **Cut examples that don't do any work.** One that only shows an off-the-shelf
  resource doesn't add anything a reader lacks. Merge a pair that make the same
  point. A contrived example damages credibility more than an absent one, so
  omit rather than force.
- **Annotate lightly.** Brief YAML comments where semantics aren't obvious, and
  `# Omitted for brevity` where you trim.
- **Cover the awkward case.** Worked examples are how a proposal proves it
  handles the topologies it claims to, so include the pathological one.

Long verified artifacts belong in collapsed blocks, so they illustrate without
bloating the document.

## Naming

Naming draws more corrections than anything but length, so treat it as work that
precedes the prose.

- Offer two or three candidates with trade-offs instead of one answer.
- Prefer terms already established in the domain. Where people contest a
  category label, say which specific things you mean instead.
- Don't borrow a loaded term unless you've adopted its semantics. Using DRA's
  "attributes" promises DRA's behaviour.
- Test a candidate by saying the sentence a reader would say aloud. "Configure
  serving" beats "configure the endpoint".
- Reject names that mislead in the degenerate case. "Leader" is wrong when
  there's one pod, and "gang" is wrong for a gang of one.
- Weigh precision against length. A more specific name is also longer, which
  costs something.
- Consider what a name connotes. "Size" suggests how big, where "count"
  suggests how many.
- One term per concept, across prose, field names, and environment variables. A
  document drifting between "stacks" and "backends" has a bug.
- No blanket bans. The repair for one inaccurate use of "orchestrator" is
  accuracy in that instance, rather than global find and replace.
- Re-derive the title and filename at the end, from what the document argues.
  Titles are pithy and descriptive. Anything that sounds like a slogan is out.

## Formatting

Reasoning belongs in prose. Reserve structure for content with real
structure.

- **No decorative structure.** Bold, headings, and bullets sprinkled in to look
  organised are a tell. A pair of paragraphs doesn't need an `###`. Resist
  subheadings in PR descriptions altogether.
<!-- vale off -->
- **Consistent heading hierarchy**, and headings that sound like an engineer
  wrote them. "The shape I'm proposing" is not one.
<!-- vale on -->
- **Ordered lists for sequential content**, prose for reasoning, bullets for
  parallel items. Parallel content gets parallel structure and matched depth.
  Labels in a parallel list run one to three words and stay consistent with each
  other.
- **Tables** where the content really is tabular. Give a table a lead-in
  sentence saying what it shows, use title case headers, and make column names
  convey their role. Avoid a hyphen as a placeholder, because it reads as
  "none".
- **Real links.** A bare `#34` doesn't resolve in a repo markdown file. Link
  code through a pinned-commit permalink so GitHub renders it as a snippet. Keep
  named projects hyperlinked.
- **No mathematical shorthand** in prose. Spell out what a Σ means.
- **Wrapping** depends on where the text appears. Hard-wrap commit messages and
  repo markdown at 80. Never hard-wrap GitHub issue or PR bodies, which reflow
  to the viewport.
- **Introduce a term before relying on it.** "The composition function" is a
  defect if the document hasn't said it's built on composition functions. Avoid
  forward references to concepts the reader hasn't met.
- **Don't make the reader infer.** If CUDA matters to two things, introduce it
  for both.

Grammar gets its own pass. Run-on sentences are the recurring offender.

## The Pass Before You Hand It Back

Nobody should spend reading time on an un-de-slopped draft. Nic, to an agent:

<!-- vale off -->
> I want you to tackle the prose cuts and AI tells edits before I spend time
> reading. This should be a tight design that reads like I wrote it. No slop, no
> filler.
<!-- vale on -->

Getting there takes two passes, which catch different things.

**Vale catches the mechanical fingerprints.** The project lints prose with the
[`vale-ai-tells`](https://github.com/tbhb/vale-ai-tells) rules. Its 110 rules
cover em dashes, filler, hedging, puffery, figurative verbs, rhetorical
scaffolding, and the rest of the phrase-level fingerprints. Some rules from the
Google developer documentation style run alongside them: `Passive`,
`Semicolons`, `ExcessiveClaims`, and `Anthropomorphism`. The rest of that style
stays off, since it bans first person and "we", which this skill relies on.

`.opencode/vale/.vale.ini` holds that config. Sync its styles once, then lint:

```bash
vale sync --config .opencode/vale/.vale.ini
vale --config .opencode/vale/.vale.ini path/to/doc.md
```

The docs under `docs/` have their own, stricter config, which CI runs.

**Fix what it reports.** Nic compared 16 linter-driven rewrites against the
originals across two design docs. He preferred the rewrite in 14 and called the
other two a tie. He never preferred the original. His summary: the edits drive
toward more succinct and direct phrasing, and that's the project style.

So comply by default, and override only when you can state the specific reason
the alert is wrong.

Be suspicious of your own sense of which alerts are noise, because it runs in
one direction: you will defend phrasing you wrote. Every alert in that
comparison that an agent had confidently called a false positive turned out to
be a real catch. The most confident defence was `StackedAnaphora` on "one
engine, one member, one pod", argued as deliberate repetition mapping onto the
API. Nic's verdict was that it "reads very AI", and the repetition was the
reason.

The exceptions are mechanical rather than matters of taste:

<!-- vale off -->
- `UniversalObject` where the quantifier is the semantics. In a matching rule,
  "satisfies every selector" is precise, and cutting "every" makes it wrong.
- `ColonUsage` firing on a document's metadata block or on YAML, where the
  parser reads a field name as prose.
<!-- vale on -->

For prose a person wrote, assume they meant it, and raise the alert rather than
acting on it.

Where a document must quote a banned construction to discuss it, wrap that
region in `<!-- vale off -->` and `<!-- vale on -->` rather than leaving an
alert.

Where your harness reports Vale's alerts as diagnostics on each edit, fix those
as part of the edit. A harness may cap how many it reports per edit, so a clean
edit doesn't mean a clean file. Run `vale` directly before handing the document
over.

**A subagent catches what Vale can't see.** Vale matches phrases. It can't tell
whether a section argues with a reviewer or invents a reason, or whether it
gives four paragraphs to something that deserves one. Dispatch a subagent
briefed with [references/de-slop-review.md](references/de-slop-review.md), plus
the audience, the purpose, and one or two existing designs under `design/` for
the project's style. Fix what it finds and repeat until a reviewer comes back
clean, because one pass rarely suffices.

Use a subagent rather than reviewing inline. Fresh context matters because the
drafting agent can't see its own reactive framing.

Where accuracy matters, run a separate pass that tests every assertion against a
skeptical reader rather than a friendly one. Prose review and correctness review
are orthogonal.

## Do Not

- Don't add AI attribution. Text published under a person's name reads as
  theirs, so write first person or drop the pronoun, and never refer to the
  author in the third person.
- Don't lose a person's edits. Where someone copy edits alongside you, their
  edits win, and any later rewrite keeps them. Re-read the file before
  editing.
- Don't summarise material the reader wrote or already knows.

## Key Principles

1. Cut 30%, then cut another 10-20%, before anyone reads it.
2. Write for the reader, never against the last draft or the last comment.
3. Never invent rationale. Ask instead.
4. Support every claim with a mechanism and a baseline, or delete it.
5. Space signals importance, so keep sections and list entries proportional.
6. A design reads as though it preceded the code.
7. Examples are real, whole, verified, and doing work.
8. Name things deliberately, with established terms that don't mislead in the
   edge case, and one term per concept.
9. Reasoning belongs in prose. Structure is for structured content.
10. Run Vale, then a fresh-context subagent, and loop until both come back
    clean.
