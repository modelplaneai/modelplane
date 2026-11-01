# Calibrating Findings

These are real review findings on Modelplane PRs, and what happened to them.
Use them to judge a finding before posting it. Maintainers decline findings
about as often as they accept them, and for consistent reasons.

## Posted and accepted

Dennis on #556, which added an early return that skipped composing the cloud
cluster:

<!-- vale off -->
> **Blocking: when this gate fires on a running cluster, Crossplane deletes the
> cluster.** The early return comes before `compose_<cloud>()`, and
> `check_devices_supported` sets a condition rather than a fatal result. So the
> desired state holds only the guard and namespaces, and Crossplane
> garbage-collects the cloud cluster XR, the ServingStack and their Usages. I
> ran the `GKEReady` case...
<!-- vale on -->

The comment gives the consequence and the code path that causes it, then
shows it happening in a test run. Nic raised the same problem independently on
the same PR.

Nic on #346:

<!-- vale off -->
> I don't think this comment is true - notably nothing seems to read the
> `modelplane.ai/gpu` label.
<!-- vale on -->

A comment that claims something the code doesn't do. Short, checkable, and
explicitly not a blocker.

Copilot on #365:

<!-- vale off -->
> cleanup() removes `${DOCKER_CONFIG:-}` on exit. If a user has DOCKER_CONFIG
> set in their environment and the script fails before it reassigns
> DOCKER_CONFIG, this can `rm -rf` the user's real Docker config directory.
<!-- vale on -->

A concrete input, a concrete failure, and a path to it.

## Wrong

Copilot left seven comments on #137 like this one:

<!-- vale off -->
> `$(MODELPLANE_LEADER_ADDRESS)` is shell *command substitution*, not
> environment-variable expansion.
<!-- vale on -->

The string is in a container's `command`, where Kubernetes expands `$(VAR)`
before the shell runs. The claim about an external tool was
confident, plausible and false, and each copy cost a maintainer a rebuttal.
Check how the tool behaves in this context before posting a claim like this,
and post the claim once rather than once per occurrence.

## Declined

A bot asked for a runtime check on a field that was about to get schema
validation:

<!-- vale off -->
> Wait isn't this guard the same thing as what #28 tracks? I don't think we
> should do it in code and also at the API level.
<!-- vale on -->

Enforce an invariant in one place.

Asked to add migration handling for an existing cache on #406:

<!-- vale off -->
> There's nothing to migrate: Modelplane is a month old at v0.3 and I'm not
> aware of any deployment with a staged cache... I'd rather not carry migration
> machinery for a hypothetical.
<!-- vale on -->

And on #362:

<!-- vale off -->
> No one is running Modelplane v0.2 - there's no one to break, no one to
> migrate.
<!-- vale on -->

Asked to extend the test-style checker to catch patterns no test used, on #482:

<!-- vale off -->
> I'd rather not grow the checker until one actually turns up.
<!-- vale on -->

A finding that adds code needs a higher bar than one that removes it, and a
hypothetical doesn't clear it.

A reviewer flagged that a library function took a caller-built map without
documenting whether it took ownership:

<!-- vale off -->
> This comment strikes me as an overzealous LLM... There's tons of places in
> Crossplane where the caller builds a datastructure to pass to a library
> module without the library documenting or defending against this.
<!-- vale on -->

Ordinary language semantics don't need documenting.

## What to take from this

- Report the evidence, not just the concern. "This could be nil" gives the
  author less than "this is nil when the XR omits `spec.workers`, which the
  schema allows".
- Say when a finding rests on precedent alone, since nobody may have reviewed
  the precedent.
- A finding that adds guards, wrappers, migration code or defensive
  documentation needs an observed failure behind it.
