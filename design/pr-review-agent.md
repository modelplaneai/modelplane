# Pull request review agent

**Status:** Draft
**Date:** November 2026
**Author:** Nic Cope

## Background

Agents write all of Modelplane's code, and most of its review. Each maintainer
points their own agent at a PR, with their own harness and agent skills. We
don't have skills of our own for finding correctness bugs, so one shared agent
could look for them on every PR as well as ours do. For style and taste, each
agent applies its owner's conventions. Skills kept in the repo would let us
agree on ours, and give the same feedback whoever reviews.

This doesn't replace maintainer review. It frees maintainers to spend review on
the insight only they have.

## Proposal

An agent reviews a PR when an org member opens it or marks it ready. To review
anyone else's PR, or to re-review after new pushes, a maintainer comments
`/review`. A re-review covers only what `git range-diff` shows changed since
the last one.

The review agent runs four reviewers with fresh context: design, prose, craft
and correctness. It checks their findings against the bar in
`.agents/skills/reviewing-pull-requests` and posts the ones that pass as one
comment review from a GitHub App. It never approves or requests changes. The
agents live in `.opencode/agents`, and the skills they load in `.agents/skills`,
where any maintainer can tune them or run the review locally:

```bash
opencode run --agent review "Review PR #556 in modelplaneai/modelplane."
```

## Security

Every PR comes from a fork, and the workflow runs on `pull_request_target`, the
trigger that gives a fork's PR access to secrets. The agent then runs the PR's
code with a shell and network, because it verifies findings by running tests and
reading upstream source. We assume the PR is malicious. Its code must never get
the model API key or any credential that can write to the repo.

**The agent runs in a Firecracker microVM.** GitHub's x86 runners expose KVM.
Escaping the VM takes an exploit in KVM or Firecracker, which is the boundary
AWS Lambda relies on, rather than in the shared kernel a container would give
us.

**The only credential in the VM can't do harm.**

- The model API key stays on the runner. A proxy there adds it to the agent's
  model requests, and forwards only the endpoint the agent calls:
  `POST /v1/messages` on Anthropic's API, or `POST /v1/chat/completions` on an
  OpenAI-compatible one. The key is one of its own, with a spend limit.
- The VM gets the job's GitHub token, so the agent can read issues and PRs with
  `gh`. It can't write anything, everything it can read is public, and it
  expires with the job.
- The App's key exists only in a second job with no model in it. That job
  validates the review and posts it.

**The VM can only connect to the proxy and the internet.** A firewall on the
runner drops its connections to the runner's other ports and private network,
and to the cloud metadata service. It drops all IPv6. It doesn't share files
with the runner. Its job arrives as a tar on a read-only disk, and it writes the
review as a tar that the post job reads in memory, never extracting it.

**A PR can't change the reviewer's setup.** The VM image, the workflow, the
agents, the skills and every other file opencode reads come from `main`. The
post job escapes any marker the agent writes, so the agent can't set what the
next re-review covers.

What's left: a malicious PR can spend API budget, and post a misleading comment
review. The spend limit bounds the first, a maintainer reads the second, and
non-members get a review only when a maintainer asks for one. PR code running
in the VM can rewrite the agent's instructions mid-review, so a PR can steer its
own review. It's still only a comment, so we accept that.

GitHub doesn't officially support KVM on its runners. About one runner in ten
during testing had none. A small workflow re-runs a review that failed for that
reason, up to twice.

## Alternatives considered

**opencode's GitHub Action.** The agent posts directly, so it holds a token that
can write to the repo.

**A container instead of a VM.** A kernel exploit in the container would give it
the runner, and the key on it. Docker can't run inside an unprivileged container
either, so the agent couldn't create a kind cluster.

**No shell for the agent.** It wouldn't need a sandbox, but it couldn't verify
findings. Unverified findings are most of the noise maintainers decline.
