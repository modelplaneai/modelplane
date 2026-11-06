# Modelplane Governance

This document defines governance policies for the Modelplane project. The
policies defined here apply to all repositories and work happening within the
[modelplaneai](https://github.com/modelplaneai) GitHub organization.

## Stewardship

Modelplane is developed in the open. Design discussions, issues, and pull
requests are public, and contributions are welcome from anyone. The maintainers
make decisions in the open, favoring the project's long-term health and its
neutrality over any single contributor or company, and we want everyone who
shows up to help to feel welcome and valued.

Modelplane was created by the team behind [Crossplane](https://crossplane.io)
and [Rook](https://rook.io) (both CNCF graduated projects), and all of its
maintainers work at Upbound today. The founding maintainers will continue to
guide the project's vision and strategy as it grows.

We want that group to grow and become more diverse. When contributors take on
more of the work described below, the maintainers will invest in them: reviewing
their work closely, involving them in design discussions, and helping them grow
into maintainers through the process in this document.

## Roles

Modelplane uses the following roles, which are described in more detail below:

- Contributors participate in the project in any way.
- Organization members are contributors who have been added to the modelplaneai
  GitHub organization.
- Maintainers have the most experience on the project and are trusted to make
  decisions and lead its growth and improvement.
- Emeritus maintainers are former maintainers who have stepped back from the
  role.

## Contributors

Anyone who participates in the project is a contributor, whether by opening
issues, submitting pull requests, reviewing code, improving the documentation,
answering questions on Slack, or providing feedback on designs. Contributors
don't need any special permissions, and they are a very valued part of the
community. Every bug report, question, and pull request makes Modelplane better,
and we are grateful for all of them.

See [CONTRIBUTING.md](CONTRIBUTING.md) to get started as a contributor.

## Organization Members

Contributors from the community can also be added to the modelplaneai
organization as a "member". This affiliation only comes with the base
permissions (triage-only) for the organization, so the requirements aren't
extensive.

Adding new members has the following benefits:
- Encourages a sense of belonging to the community and encourages collaboration
- Promotes visibility of the project by being displayed on each member's profile
- Demonstrates tangible growth of the community and adoption of the project
- Allows issues to be assigned to the user

Community members who wish to become members of the modelplaneai organization
should meet the following requirements, which are open to the discretion of the
core maintainers:

- Have [enabled 2FA](https://github.com/settings/security) on their GitHub
  account.
- Have joined [Modelplane Slack](https://slack.modelplane.ai).
- Are actively contributing to the Modelplane project. Examples include:
  - opening issues
  - providing feedback on the project
  - engaging in discussions on issues, pull requests, Slack, etc.
- Have reached out to one current modelplaneai organization member who has
  agreed to sponsor their membership request.

To request membership, open an issue in the
[modelplane](https://github.com/modelplaneai/modelplane/issues) repository that
links to some of your contributions and names your sponsor. Your sponsor then
comments on the issue to confirm their sponsorship, and a core maintainer sends
the invitation to join the organization. New members should set the
visibility of their modelplaneai organization membership to public.

## Maintainers

Each repository in the modelplaneai organization is allowed its own unique set
of maintainers. Maintainers have the most experience with the given repo and are
expected to have the knowledge and insight to lead its growth and improvement.

The maintainers of the [modelplane](https://github.com/modelplaneai/modelplane)
repository are the project's core maintainers. In addition to their
responsibilities for that repository, the core maintainers are responsible for
the overall health of the project, decide issues that affect multiple
repositories or the project at large, and own this governance. This includes
making sure the project's repositories stay healthy, so the core maintainers
decide when to create new repositories in the modelplaneai organization and when
to archive ones that are no longer maintained. The core maintainers also handle
security reports, following the project's [security policy](SECURITY.md).

It's likely that the role of the core maintainer becomes more about
organization-wide governance over time and we will consider changing the
governance to reflect that, possibly by adding a distinct role like a steering
committee.

In general, adding and removing maintainers for a given repo is the
responsibility of the existing maintainer team for that repo and therefore does
not require approval from the core maintainers. However, in rare cases, the core
maintainers can veto the addition of a new maintainer by following the [conflict
resolution process](#conflict-resolution-and-voting).

Responsibilities include:

- Strong commitment to the project, generally spending at least 25% of their
  time working on Modelplane (~1.25 business days per week)
- Participate in design and technical discussions
- Contribute non-trivial pull requests
- Review, approve, and merge others' pull requests, upholding the quality of the
  code base and making sure they reach a resolution at the right pace
- Regularly triage GitHub issues
- Monitor [Modelplane Slack](https://slack.modelplane.ai) (delayed response is
  perfectly acceptable), particularly for the area of your repository
- Participate in the conflict resolution and voting process at the repository
  scope when necessary, including adding and removing maintainers

The current list of maintainers for each repository is published and updated in
each repo's [MAINTAINERS.md](MAINTAINERS.md) file, along with each maintainer's
GitHub ID and company or organization.

### Becoming a Maintainer

New maintainers come from contributors who are already doing much of the work
listed above. Reviewing other people's pull requests is one of the best ways to
start, because it builds the same understanding of the code base and our
standards that maintainers rely on.

Beyond consistently fulfilling the maintainer responsibilities to some degree,
you need to demonstrate the following:

- Domain expertise in the area of focus for the repository
- Willingness to contribute and provide value to all areas of the repository's
  code base, beyond simply the direct interests of your organization, and to
  consistently promote the overall charter and vision of the entire Modelplane
  project.
- Be extremely proficient with the languages, tools, and frameworks used in the
  repository
- Ability to write good solid code and tests, and an understanding of the
  project's code base, coding style, and how the team works

Beyond your contributions to the project, consider:

- If your organization already has a maintainer on a given repository, more
  maintainers from your org may not be needed. A valid reason, however, is
  "blast radius" to get more coverage on a large repository.
- Becoming a maintainer generally means that you are going to be spending
  substantial time (>25%) on Modelplane for the foreseeable future.

If you are meeting these requirements, express interest to the repository's
existing maintainers directly.

- We may ask you to do some PRs from our backlog.
- As you gain experience with the code base and our standards, we will ask you
  to do code reviews for incoming PRs.
- After a period of approximately 2-3 months of working together and making sure
  we see eye to eye, the repository's existing maintainers will confer and
  decide whether to grant maintainer status or not, as per the voting process
  described below. We make no guarantees on the length of time this will take,
  but 2-3 months is the approximate goal.
  - This time period is up to the discretion of the existing maintainer team and
    it is possible for new maintainers to be added in a shorter time period than
    this general guidance.

To formalize the addition of the new maintainer, the existing maintainer team
will then add them to the repo's MAINTAINERS.md file, as well as the appropriate
GitHub team that allows maintainer permissions to the repo, including merging
pull requests into protected branches.

### Maintainers for New Repositories

The guidelines of collaborating for 2-3 months may not be feasible for when a
new repository is being created in the modelplaneai organization. For new
repositories, the core maintainers can choose to "bootstrap" the maintainer team
as they see fit.

## Emeritus Maintainers

If a maintainer is no longer interested or cannot perform the maintainer duties
listed above, they should volunteer to be moved to emeritus status. In extreme
cases this can also occur by a vote of the maintainers per the voting process
below.

The emeritus maintainer should be removed from the GitHub team that grants
maintainer permissions, and moved to the emeritus section of the repo's
MAINTAINERS.md file, where we recognize their contributions to the project.

## Decision Making

Modelplane is a discuss-first project, and most decisions are made in the open
as part of the normal flow of work. Changes start as a public discussion, so
that we can agree on the approach before anyone spends time building it.
[CONTRIBUTING.md](CONTRIBUTING.md) describes how to propose a change.

Proposals are decided by lazy consensus among the maintainers. Once a maintainer
supports a proposal, work can start on it, as long as no other maintainer
objects after a reasonable time for review.

The work itself is accepted through pull requests. A pull request is merged
after it is approved by at least one maintainer of the repository who is not the
author. It shouldn't be merged just because nobody objected to it.

### Conflict resolution and voting

In general, it is preferred that technical issues and maintainer membership are
amicably worked out between the persons involved. If a dispute cannot be decided
independently, the maintainers at the appropriate scope can be called in to
decide an issue. If that group cannot decide an issue themselves, the issue will
be resolved by voting.

Issues can be resolved or voted on at different scopes:

- Repository: When an issue or conflict only affects a single repository, then
  the maintainer team for that repository should resolve or vote on the issue.
  This includes technical decisions as well as maintainer team membership.
- Project: If an issue or conflict affects multiple repositories or the
  Modelplane project and community at large, the core maintainers should resolve
  or vote on the issue.

The voting process is usually a simple majority in which each maintainer within
the voting scope gets a single vote. The following decisions require a super
majority (at least 2/3 of votes), all other decisions and changes require only a
simple majority:

- Updates to governance or the charter by the core maintainers
- Removals of maintainers by the repository's current maintainer team
- Vetoes of maintainer additions by the core maintainers

For formal votes, a specific statement of what is being voted on should be added
to the relevant GitHub issue or PR. Maintainers should indicate their yes/no
vote on that issue or PR.

After a suitable period of time (goal is by 5 business days), the votes will be
tallied and the outcome noted. If any maintainers are unreachable during the
voting period, postponing the completion of the voting process should be
considered.

## Updating the Governance

This governance will likely be a living document and its policies will therefore
need to be updated over time as the community grows and the project matures. The
core maintainers have full ownership of this governance and of the project's
[charter](CHARTER.md), and only the core maintainers may make updates to them.
Changes can be made at any time, but a super majority (at least 2/3 of votes) is
required to approve any updates.

## Acknowledgements

This governance is adapted from the [Crossplane
governance](https://github.com/crossplane/crossplane/blob/main/GOVERNANCE.md).
