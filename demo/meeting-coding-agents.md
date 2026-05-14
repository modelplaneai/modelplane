# Automated Coding Agents — Pilot Requirements

*This team will be pilots for automated coding agents. Capturing requirements and wishlist for future build.*

## Desired Flow

| Item | Details | Notes |
|------|---------|-------|
| **Trigger** | How does a task start? Ticket assigned, label added, slash command, scheduled? | |
| **Ticket scoping** | What types are suitable? Bugs, small features, refactors, tests, docs? | |
| **Context gathering** | What does the agent need? Ticket, docs, repo, related PRs, Slack threads? | |
| **Branch/PR workflow** | Auto-create branch? Draft PR? Who reviews? Auto-merge criteria? | |
| **Human checkpoints** | Where must a human approve before the agent proceeds? | |
| **Completion criteria** | When is it "done"? PR opened, tests pass, review approved? | |

## Security & Trust

| Item | Details | Notes |
|------|---------|-------|
| **Code access** | Full repo or sandboxed? Which repos? | |
| **Secrets** | Can agent see .env, credentials, infra configs? | |
| **Sandbox** | Where does code run? Container, VM, local? How isolated? | |
| **Audit** | What needs logging? Prompts, responses, tool calls, file changes? | |
| **Blast radius** | Worst case if agent goes wrong? Rollback plan? | |

## Scale & Infrastructure

| Item | Details | Notes |
|------|---------|-------|
| **Volume** | How many tickets/day for the agent? | |
| **Concurrency** | Max parallel agent sessions? | |
| **Day vs night** | Interactive assist by day, batch processing overnight? | |
| **GPU strategy** | Reserved vs spot for background work? | |
| **Cost** | Acceptable budget per ticket or per month? | |

## Minimum Viable Product

| Item | Details | Notes |
|------|---------|-------|
| **Simplest useful version** | What's the smallest thing that would be valuable? | |
| **Must-haves** | Top 3 non-negotiable features? | |
| **Existing infra** | CI/CD, test suites, linters, review tools already in place? | |
| **Timeline** | When would they want to start dogfooding? | |
| **Success metric** | Tickets closed, dev hours saved, PR quality? | |

## Wishlist

| Item | Details | Notes |
|------|---------|-------|
| | | |
| | | |
| | | |
| | | |
| | | |
