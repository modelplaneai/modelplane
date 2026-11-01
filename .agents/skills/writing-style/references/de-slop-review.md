# De-slop Review

A briefing for a fresh-context reviewer. Dispatch a subagent with this file, the
document under review, and the answers to the three questions below. Read it
yourself only to check what you're asking the reviewer to do.

Vale already covers the phrase-level fingerprints, so don't spend the reviewer's
attention there. Run Vale as SKILL.md describes first and fix what it reports.
This review covers the defects Vale can't detect, because they depend on what
the document means rather than which words it uses.

## Briefing the Reviewer

Tell the reviewer:

1. **Audience.** Who reads this document.
2. **Purpose.** What decision it serves.
3. **Project style.** Point at one or two existing designs under `design/`, so
   the reviewer can compare rather than guess.

Then give it the instructions below.

## Instructions for the Reviewer

You're reviewing a technical document drafted by an LLM for Modelplane's
maintainers. They read every word and delete whatever the reader doesn't need.
Your job is to find what they would object to, before they spend time on it.

Read the whole document first. Then work through the checks below. Quote the
offending text for each finding, because a claim the author can't locate is a
claim they'll ignore.

### 1. Reactive framing

The worst and most common defect. The document argues with something the reader
can't see: a reviewer's objection, or the document's own earlier draft.

Look for sentences that only make sense to somebody who followed the drafting
conversation. Symptoms include a passage litigating a distinction the reader
never raised, comparisons against an earlier approach the reader can't see
("this replaces an earlier…", "X isn't a…", "not a Y on top of a Z"), and a
section that reads as a rebuttal rather than an explanation.

Report each one, and say what the section would look like written from first
principles for the stated audience.

### 2. Invented specifics

Flag anything the author couldn't know or the reader can't verify:

- A reason for a decision that reads as reconstructed rather than remembered.
- A field, resource, flag, or API that may not exist, above all in a third
  party's project.
- A term presented as established usage.
- A rule or policy that nothing enforces.
- An example that looks composed from memory rather than copied from something
  tested.

Check these against the repo and upstream sources where you can. Where you
can't, say which claims you couldn't confirm rather than passing them.

### 3. Unsupported claims

For every comparative or evaluative statement, ask whether the document supplies
the mechanism and the baseline. "X is ahead of Y" needs to say how, and what Y
does. Flag:

- Confident claims with no traceable source.
- Evaluative adjectives applied to one thing and withheld from its peers.
- Word choices implying a conclusion the facts don't support.
- Rhetorical absolutes.
- A partisan frame in what should be a neutral comparison.

### 4. Proportion

Space is how a reader judges what matters, so measure it. Report any section or
list entry noticeably larger than its siblings, and say whether its importance
justifies that. Watch for a topic inflated because it came up late in drafting.
Check that real content isn't hidden in parentheses, and that no list entry
bundles several concepts.

### 5. Flow and coherence

Read the document end to end as somebody meeting it for the first time:

- Does an opening paragraph make a promise that the section never delivers on?
- Does the document use a term before introducing it?
- Do two passages say the same thing in different places?
- Is there a paragraph left over from an earlier framing that no longer applies?
- Does the ordering respect what the reader knows so far?
- Are there run-on sentences?

### 6. Structure

Check the document against the design-docs skill's skeleton: executive summary,
background, goals, proposal, alternatives considered, with future improvements
where they apply. Report:

- An Open Questions or Risks section, when a design should take a position.
- A trailing positions, recommendations, or summary section. Opinions belong
  beside the thing they concern.
- Alternatives scattered inline rather than gathered in their own section.
- Headings, bold, or bullets used for decoration. Reasoning belongs in prose.
- Links to issues or PRs in the same repo, which expose the document's
  chronology.
- Goals scoped to a release rather than to the project, or framed as the absence
  of a problem rather than as what gets delivered.

### 7. Length

Say where 20% could come out with no loss of meaning, and quote the candidates.
Assume the draft is still too long even after the author has cut it once. If you
find nothing, say so plainly rather than inventing a suggestion.

## Report Format

```
## Verdict
Ship, or needs work. One sentence.

## Findings
For each, in priority order:
- **[check number and name]** — quoted text. What's wrong, and what to do about it.

## Could not verify
Claims you were unable to confirm, with what you tried.

## Length
Where the next 20% comes from, quoted.
```

## Reviewer Constraints

- Don't edit the document. Report, and leave the file alone.
- Don't rewrite passages wholesale unless asked. A quoted line and a sentence
  about the fix is more useful than a replacement draft.
- Don't restate the phrase-level matches Vale already caught.
- Don't open with praise or close with encouragement. The author reads the
  findings.
- Don't soften a finding to seem agreeable. A defect you mention gently goes
  unfixed.
