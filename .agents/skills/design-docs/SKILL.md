---
name: design-docs
description: Write and review Modelplane design documents - proposals and designs under design/. Use when drafting, authoring, or structuring a design doc, and when reviewing, refactoring, tightening, or cleaning up someone else's design doc, especially an AI-written one that reads poorly. Also use when asked whether a design doc is good, or to bring one up to the standard of a gold-standard example.
---

# Design Docs

A design doc exists to win agreement on a problem and a proposed solution. Its
readers are peer maintainers who know the project the doc concerns. They often
will not know the problem itself, nor the technical area around it. That area
might be anything from a serving engine to a wire protocol. A good design doc
gives that reader the problem, the case that the proposal solves it, and the
alternatives it beat. They should finish it ready to approve the direction or
push back on it.

Everything here serves that reader. A rule that rests only on convention or an
author's taste is not in this skill.

## When to Use This (vs other skills)

Use **this skill** for the structure and content of a design doc: whether it
frames its problem and argues its solution, and how well it handles its
alternatives. It
applies both to writing one from scratch and to fixing a poor one.

Use **writing-style** for sentence-level voice and the AI-tell linting pass. A
doc that fixes its structure can still read AI, so clear the writing-style Vale
and de-slop pass before calling the doc done.

Modelplane's design docs live under `design/`, and `design/design.md` is a
good example of the standard below.

## The Standard

These describe a good design doc. They also serve as the rubric to score one
against, and the checklist to refactor one toward.

1. **Leads with the problem.** The reader reaches the proposal already
   understanding the problem. A proposal makes sense only against a problem the
   reader already feels. Open with the solution and they read the rest working
   out what it is for.

2. **Is self-contained.** Everything the argument rests on is in the doc. Links,
   in-repo or out, are optional depth the reader may follow but never has to. A
   design that makes sense only alongside issues, PRs or review threads has put
   its content where the reader may never go.

3. **Covers the domain, assumes the project.** Write for a maintainer who knows
   the project but not the problem space. Explain the one property the proposal
   rests on, and no more. A peer new to the area cannot buy into a problem
   stated in its terms, and spelling out what a maintainer already knows wastes
   them.

4. **Reads as the end state, in the present tense.** Describe the design as the
   first and only position, not the latest of several. Document status (`draft`,
   `accepted`) is fine. A record of how the code got here is not: no "proven but
   unmerged", no proof-of-concept branch name, no "this replaces an earlier
   approach". That history makes the reader reconstruct the past instead of
   reading the design. Discarded thinking goes to Alternatives, gathered there
   rather than scattered inline.

5. **Mechanics in broad strokes, API and UX in full.** Show the interface a user
   or caller touches. That covers the YAML they write, the API objects they see,
   the command they run, and the response they get. Show it with real, whole
   examples. Keep the internals of a controller, function, or pipeline out. The
   doc drives direction and buy-in, and internals only distract from that. They
   belong in the repo, where they change with the code.

6. **Proportion.** A section's size matches its importance to the whole doc, not
   how recently it came up or how much the author enjoyed it. Readers weigh a
   topic by the room it gets. An oversized section misleads them. A background
   or future-work section much longer than the proposal points to a problem to
   fix.

7. **Takes a position on everything it records.** Give the reviewer something
   concrete to approve or push back on. A point important enough to record at
   design-doc level gets a position. Leave out a point the team could approve
   and settle later. Do not add an Open Questions section, which an LLM above
   all will fill with invented questions, and do not pose questions to the
   reader. When a choice is close, pick one and put the runner-up in
   Alternatives.

8. **Alternatives honest and in their section.** Each real alternative, the
   current system included, gets a fair account. Say what it is and what is good
   about it, then why the proposal is better anyway. No strawmen, and no arguing
   an objection inline where it breaks the thread. A reader who wonders "why not
   X?" should find X in one place.

9. **Examples real and whole.** Reuse a tested artifact, show the whole object
   rather than a fragment, and carry one example through the doc. An example
   does more for the reader than the prose around it, and a fabricated one
   misleads because its detail invites trust. When you cannot build a credible
   one, say so rather than invent.

10. **As short as the three jobs allow.** Frame the problem, argue the solution,
    cover the alternatives, then stop. Remove any paragraph the reader does not
    need. Length costs the reader time, and it hides the argument. A design for
    one feature that runs past roughly twice the length of a tight peer design
    is a finding. Either it covers a large domain, or it is over-produced, so
    justify it or cut it.

## Authoring

Before writing, settle who the doc is for and what decision it serves. Identify
the problem space the reader will not know, and state what falls outside the
doc's scope. Ask rather than guess.

Then follow the skeleton. Most docs use some of it. Drop any section with
nothing to say.

1. **Title.** Short and descriptive. Derive it again at the end from what the
   doc argues.
2. **Status, date, author.** `draft` or `accepted`, the date, and who wrote it.
   Use "I" for one author, "we" for co-authors.
3. **Summary** (longer docs). A few paragraphs that give the whole picture, so a
   busy reader can stop there with the problem and the proposal clear. Put the
   primary example here.
4. **Background.** The problem, what stands today, and the prior art. Here the
   doc leads with the problem (standard 1) and covers the domain (standard 3).
5. **Goals.** What the doc delivers, and what it leaves out. A few bullets,
   stated positively.
6. **Proposal.** The concrete design. Show the API and UX in full (standard 5),
   with a worked example per case, including the awkward one.
7. **Future improvements** (optional). Out of scope now, plausible later. All
   speculative material belongs here, and stays proportional (standard 6).
8. **Alternatives considered.** What else you weighed, and why it lost (standard
   8).

Write the background as the story of how we got here and why the status quo fits
poorly now. The reader should reach the proposal already feeling the problem.
Cover only the property the proposal rests on. Respect what came before: state
the shortcoming and the new constraint that exposes it, without calling the old
system bad.

Then cut and de-slop. A first draft runs long. Cut once for what the reader can
reconstruct, then again for what remains. Trade paragraphs of prose for a worked
example wherever one carries the point. Run vale over the whole doc and a
fresh-context de-slop review, and fix both until they come back clean.

For voice, the section-by-section skeleton in more depth, and worked examples of
the target, read [references/voice.md](references/voice.md) when drafting from
scratch.

### Writing the End State

A design records direction. It must not actively mislead, though it may show
things not yet built (standard 4).

- Don't link issues or PRs in the same repo. They expose the doc's real
  chronology, and a design should read as the first artifact in the repo. Link
  generously outward to upstream issues, specs, talks and prior art.
- Don't link a durable doc to a scratch file you'll delete.
- A superseded doc isn't rewritten. It gets a one or two sentence preface
  saying it remains for historical context. At some point a design stops
  absorbing revisions and gets extended by supplementary designs.
- Goals appear positively, as what gets delivered, and at project level rather
  than scoped to a release.
- Leave out the limitations currently blocking the end state. Where the
  mechanism isn't decided, stay deliberately vague and state the requirement.
- Opinions belong inline beside the thing they concern.

### Examples

Beyond standard 9:

- **Calibrate realistically.** Selectors ask for what a model actually needs,
  and examples reflect the scale users really run.
- **Cut examples that don't do any work.** One that only shows an off-the-shelf
  resource doesn't add anything. Merge a pair that make the same point.
- **Annotate lightly.** Brief YAML comments where semantics aren't obvious, and
  `# Omitted for brevity` where you trim.
- **Cover the awkward case.** Worked examples are how a proposal proves it
  handles the topologies it claims to, so include the pathological one.
- **Collapse long artifacts,** so they illustrate without bloating the doc.

## Refactoring

Refactoring a poor doc, often an AI-written one, is surgery on the document's
shape more than a copy edit. Read it against the standard before touching it,
and write the diagnosis down: for each point, whether it holds and where it
fails. That record is the before-state, and the feedback for the author. A fresh
reader catches what the author and the drafting agent cannot, so run the
diagnosis from a fresh context, or have another agent do it, briefed with the
standard and one strong example.

Fix the structure before the prose, in this order, because fixing a sentence you
later cut wastes effort:

1. **Buried or missing problem** (standard 1). Lift the sharp problem statement
   into the opening of the background. If it sits only in the PR description,
   move it into the doc.
2. **Internals where API and UX belong** (standard 5). Cut controller and
   pipeline internals to broad strokes, and show the interface in full. Usually
   the largest cut.
3. **Leaked chronology** (standard 4). Rewrite to the present-tense end state,
   and move a discarded design to Alternatives.
4. **Off proportion** (standard 6). Cut the oversized section, then its
   neighbours.
5. **The doc argues with itself or a reviewer** (standards 4 and 8). Move the
   substance to Alternatives and rewrite the section positively, from first
   principles.
6. **Open questions or hedged non-decisions** (standard 7). Make the call, or
   cut the point if it does not warrant one.
7. **Thin or unfair alternatives** (standard 8). Give each a fair account and an
   honest reason it lost.
8. **Fabricated or fragmentary examples** (standard 9). Replace with a real,
   whole object.

Prefer surgery to a rewrite. Keep the author's structure and content and move it
where you can. The author should still recognise the doc. A section built on a
framing error usually needs redoing from first principles, because patched prose
drifts into incoherence.

Then de-slop, and do not skip it. Run vale and a fresh-context de-slop review,
and loop until both come back clean. A doc that meets the standard but reads as
though an LLM wrote it is not done.

Prove it improved. Score the refactor against the standard again, blind where
you can. Give a fresh agent the rubric and a strong example without saying which
version is which. The refactor succeeds when it scores at or above the exemplar
on the same rubric the diagnosis used.
