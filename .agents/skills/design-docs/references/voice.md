# Voice and Structure

What the target looks like. Read this when drafting from scratch. The failure modes in SKILL.md
matter more, because they describe where drafts actually go wrong. This file
describes where they should end up.

## Voice

### First person, direct

Write in first person. "I propose…", "I believe…", "I think…", "I'd argue…".
State positions plainly instead of hedging behind passive voice or committee
language. The reader should always know what the author thinks and why.

Use "I" rather than a collective "we" for a document one person authored. Where
uncertain, say so ("I'm not sure how real or widespread these concerns are", "I
suspect…") while still taking a position.

### Conversational authority

The tone is an experienced engineer talking to peers. Informal enough for
contractions and for phrases like "a lot of", "pretty complex", "not so bad".
Rigorous enough for precise technical detail, concrete numbers, and YAML.

<!-- vale off -->
Avoid corporate voice ("leverage synergies", "drive alignment"), academic voice
("it is posited that"), and stacked hedges ("it might perhaps be worth
considering").
<!-- vale on -->

### Empathy for the reader

Assume a smart reader who lacks your context. Give enough background for
somebody joining the conversation, without over-explaining what peers know.
Where a concept surprised you, say so, then explain what made it click.

Take care with `obvious`, `obviously`, `clearly`, and `of course`. What's
obvious to a senior engineer often isn't obvious to a junior one, and these
words risk making a reader feel bad for not already knowing. This isn't a hard
ban, since some things really are obvious. Default to stating the thing rather
than editorialising about how apparent it should be.

Use analogies to connect unfamiliar ideas to familiar ones. "An MRD is to an MR
CRD as an XRD is to an XR CRD." "Think of this like AWS Lambda." "`flake.nix` is
a bit like a Makefile backed by a snapshot of nixpkgs."

### Release verbs point forward

<!-- vale off -->
Use "ship" for the act of releasing, as intent or as something still ahead.
"We need to ship X." "Should v0.1 ship with something simpler?" "Biasing toward
shipping rather than another review round."

Don't use it to describe what a product has or does. "Modelplane ships a
Traefik gateway" and "nobody ships this layer today" are LLM phrasings. The fix
is the specific verb: Modelplane *installs* a Traefik gateway, *includes* the
connector runtime, *composes* the HTTPRoute. Naming the real action is more
precise anyway, which is what the linter asks for when it flags the word.
<!-- vale on -->

Frequency isn't the problem, so don't overcorrect into avoiding the word. The
difference is the direction it points.

The same test applies to any verb of delivery or provision with a product as its
subject. Say what the software does, and reserve the release verb for releases.

## Structure

### Standard sections

Most documents follow this skeleton, though few use all of it:

1. **Title.** Short, descriptive, re-derived at the end from what the document
   argues.
2. **Status, date and author.** As the existing designs under `design/` do.
3. **Executive summary** (longer docs). A few paragraphs giving the whole
   picture, so a busy reader can stop there. Include the primary example here.
4. **Background.** What the reader needs to follow the proposal, including
   prior art and the problem.
5. **Goals.** What the document tries to achieve, and what falls outside it.
6. **Proposal.** The concrete design.
7. **Workstreams** or **Migration plan** (optional). What makes it real.
8. **Future improvements** (optional). Out of scope now, plausible later. This
   is where speculative material belongs.
9. **Alternatives considered.** What else you evaluated, and why it lost.

Shorter one-pagers skip the executive summary and run background, proposal,
alternatives. Drop any section with nothing to say.

There is no Open Questions section. See SKILL.md.

### Front-load the destination

Before the background starts building the case, give the reader a sentence or
two saying where this goes. It can close a brief introduction or open the
executive summary. Knowing the thesis turns the background from "where is this
going?" into "I see why this matters".

### Background is a story

Background sections run long relative to their neighbours, because they tell
the story of how we got here and what changed. They link out to
prior work so the reader can go deeper. A good one has the reader nodding along,
thinking "yes, I see why this is a problem", before the proposal starts.

Long is not the same as padded. Each paragraph still has to do work, and the
section still gets both cutting passes. Where the background outgrows the rest
of the document by a wide margin, that's a finding.

Background also needs the case that forced the decision. Where prefill/decode
disaggregation was the litmus test for a new design, the background covers it.
Showing the status quo in YAML often beats describing it.

### Goals are crisp

Goals sections are short, a few bullets or a brief paragraph. They state what's
in scope and what isn't. "It's not a goal to…" appears often.

Pitch goals at project level rather than at a release, and state them
positively, as what gets delivered. Keep build detail out. Goals also frame
ambition: "My goal with this document is to sketch out…" sets different
expectations from "The goal of this proposal is to have Upbound Crossplane
install only the CRDs customers actually use."

### Proposals are concrete

Proposals include YAML, protobuf, API specs, and diagrams. Show the API rather
than describing it. Walk through the user experience step by step:

<!-- vale off -->
> Assume you've installed provider-aws-ec2 with mrdActivationPolicy: Manual…
> You then install a Configuration that depends on provider-aws-ec2. The
> Configuration would consider its dependency satisfied, because the Provider is
> installed. The Configuration wouldn't be usable though…
<!-- vale on -->

Where a proposal claims to handle a set of cases, prove it with a worked example
per case, including the pathological one.

### Code is for developer experience

Design documents aren't the place to sketch how the code works inside. Leave out
Go or Python unless the code is itself the design, as with a library API, an
SDK, or a developer experience where what the user writes is the thing you're
designing.

Show code where the argument concerns how something would feel to use. Leave it
out where it would only illustrate the innards of a controller or a reconciler.
Those belong in the repo.

### Alternatives are fair

Give each alternative a fair hearing. Say what it is, what's good about it, then
why the proposal is better, without resorting to strawmen. This applies doubly
where the alternative is the current system.

Include an alternative only when it adds something. Not every discarded idea
needs an entry, and a low-value one is better deleted. Keep entries roughly the
same length as each other.

## Rhetorical patterns

### Build the case before the proposal

Structure the document so the reader reaches the proposal already understanding
the problem. By the time they hit "I propose…", it should feel like the natural
next step.

### Acknowledge trade-offs

Don't pretend the proposal is perfect. State the downsides:

> PR code running in the VM can rewrite the agent's instructions mid-review, so
> a PR can steer its own review. It's still only a comment, so we accept that.

### Use concrete numbers

Prefer specific numbers to vague claims:

> 90 superfluous CRDs × 1.75 MB = 157.5 MB of API server memory. That's not so
> bad for one control plane, but across 10 control planes it's 1.5GB of wasted
> memory.

Numbers have to be real, and traceable to something you measured or read. The
number above comes from a measured CRD size. A plausible figure invented to make
a paragraph concrete is the worst kind of fabrication, because its precision is
what makes the reader trust it. Where you don't have a number, describe the
effect and say you haven't measured it. Never estimate hardware costs,
latencies, or utilisation rates to strengthen an argument.

### Respect what came before

Assume whoever designed the previous system did what they could with what
they knew, and assume they're reading this. Catalogue concrete shortcomings
("the ingestion pipeline adds substantial machinery for a debugging tool")
without calling the old system bad or broken. Focus on what changed, meaning new
requirements or constraints, that makes the old approach a poor fit now. Explain
why it's time to move on rather than belittling the past.

### Address skeptics directly

Anticipate objections and answer them inline. State the likely objection in the
reader's voice, take it seriously, then answer it. Don't introduce each one with
the same formula.

Note the boundary with reactive framing in SKILL.md. Answering an objection a
reader would independently raise is good. Rewriting a section to argue with a
reviewer who already raised one is not.

### Frame decisions as preferences

"I'd prefer…", "I lean toward…", "I don't feel very strongly" signal confidence
in the reasoning without shutting down discussion.

Where constraints forced a choice rather than taste, say that instead. Traefik
is the only thing meeting the requirements today. Framing it as a favourite
misleads.

### Reference prior art

Link to and summarise relevant prior work, such as upstream issues, old design
docs, community discussions, and conference talks. Show awareness of a problem's
history.

Link outward, generously. Don't link to issues or PRs in the same repo, for the
reasons in SKILL.md.

## Formatting

### Paragraphs and flow

Write flowing paragraphs rather than bullet-heavy outlines. Use bullets for
lists of concrete items such as API fields, workstreams, or goals, and prefer
prose for reasoning and narrative. Paragraphs run three to six sentences.

### Headings

Use descriptive headings. "Namespace Composite Resources" beats "Proposal Item
1". H2 for major sections, H3 for subsections, applied consistently.

### Code and YAML examples

Include examples inline where they clarify the proposal. Add brief comments
where semantics aren't obvious. Keep examples focused, trimming irrelevant
fields with `# Omitted for brevity`. Show whole objects rather than fragments,
and reuse tested manifests from the repo.

### Links

Use inline links in prose, or reference-style links at the bottom for upstream
design docs. Make issue references real URLs, since a bare `#34` doesn't resolve
in a repo markdown file.
