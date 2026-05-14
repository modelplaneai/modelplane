# Self-Hosted Coding Model — Requirements Discussion

## Current State

| Item | Details | Notes |
|------|---------|-------|
| **Current tools** | What coding assistants are teams using today? | |
| **Pain points** | Cost, latency, privacy, context limits, reliability | |
| **Privacy / compliance** | Can code leave the network? Regulatory constraints? | |

## Model Requirements

| Item | Details | Notes |
|------|---------|-------|
| **Model preferences** | Which models do they like for coding? | |
| **Quality bar** | Would an open model (7B/32B) be good enough? For which tasks? Where do they need frontier? | |
| **Context window** | Single file, whole repo, monorepo? How much context matters? | |
| **Tool use / agents** | Tool calling, file editing, terminal access? Or just chat/completions? | |

## Developer Experience

| Item | Details | Notes |
|------|---------|-------|
| **IDE / interface** | VS Code, JetBrains, terminal, Cursor, other? | |
| **Workflows** | Autocomplete, chat, inline edit, code review, test generation? | |
| **Response time** | Acceptable TTFT for interactive use? | |
| **Reliability** | What uptime do they need? Occasional downtime OK? | |

## Scale & Usage

| Item | Details | Notes |
|------|---------|-------|
| **Team size** | How many devs would use this? | |
| **Concurrency** | Peak concurrent sessions estimate? | |
| **Usage patterns** | Daytime interactive, overnight batch, both? | |
| **Spot tolerance** | OK with spot GPUs for cost savings? Background tasks only? | |

## Infrastructure & Cost

| Item | Details | Notes |
|------|---------|-------|
| **GPU preference** | Dedicated (reserved, always-on) vs elastic (scale to zero, pay per use)? | |
| **Cost target** | Acceptable per dev/month? Compared to current tool spend? | |
| **Hosting** | On-prem, GKE, other? Existing clusters to leverage? | |
| **Metrics / observability** | What do they want to track? Usage, latency, cost, quality? | |

## Rollout

| Item | Details | Notes |
|------|---------|-------|
| **Pilot group** | Who tries it first? How many devs? | |
| **Success criteria** | How do they know it's working? | |
| **Timeline** | When would they want to start? | |
| **Feedback loop** | How do they report issues / request changes? | |
