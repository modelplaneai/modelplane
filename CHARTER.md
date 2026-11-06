# Modelplane Charter

## Overview

**Modelplane is the open control plane for AI inference.**

**Any model. Any engine. Any infrastructure. One inference system.**

A serving engine runs a model on a node. A serving stack runs engines on a
cluster. Modelplane runs the fleet: it provisions clusters and node pools,
installs the serving stack on each, stages model weights, places model replicas,
and programs routing. The clusters do the serving; Modelplane makes them behave
as one inference system.

Modelplane provides the declarative APIs and continuously reconciling
controllers that turn infrastructure across clusters, clouds, and accelerators
into a unified inference system.

Modelplane runs multiple model deployments on one fleet and supports different
controls for each. It serves one tenant: a group that can share hardware,
potentially spanning many teams, departments, and business units.

## Architectural Principles

The following principles guide Modelplane's design and evolution.

1. **Declarative by design.** Desired state is expressed through APIs and
   continuously reconciled. Modelplane converges infrastructure toward declared
   intent rather than relying on imperative workflows.

2. **Infrastructure independent.** Any model, serving engine, serving topology,
   accelerator, and infrastructure provider can participate. No particular
   implementation is privileged by the architecture.

3. **Composable, not monolithic.** Modelplane provides built-in scheduling,
   routing, caching, and reconciliation mechanisms sufficient to operate a
   complete inference fleet. Alternative implementations compose through open
   interfaces. Higher-level optimization belongs in controllers built against
   Modelplane's APIs and telemetry.

4. **Kubernetes-native.** Modelplane builds on Kubernetes resource APIs,
   reconciliation patterns, and extensibility conventions. Its APIs follow
   Kubernetes versioning and compatibility practices.

5. **Open and vendor-neutral.** Modelplane is an open-source foundation for an
   ecosystem, not a proprietary service interface. Its APIs and architecture
   remain usable independently of any vendor, commercial offering, or
   infrastructure provider.

## What Modelplane Is

Modelplane is the control plane for an inference fleet: many models, many
clusters, and many clouds operating as one inference system.

Its responsibility is to provide the mechanisms that realize declared intent and
continuously maintain the desired state of that fleet.

The following are in scope:

- **Universal infrastructure support.** Support for any model, serving engine,
  serving topology, accelerator, and infrastructure provider.
- **Stable declarative APIs.** A resource model for clusters, deployments,
  replicas, services, endpoints, caches, and gateways, expressed as Kubernetes
  resources and unchanged when models, engines, accelerators, or providers
  change.
- **Fleet reconciliation.** Controllers that provision clusters and node pools,
  install per-cluster serving stacks, stage model weights, place replicas, and
  program the routing data plane.
- **Unified inference access.** Gateways fronting the fleet and each cluster,
  exposing a unified inference API.
- **Extensible gateways.** Extension points for authorization, classification,
  guardrails, and other request-processing capabilities.
- **Composable fleet mechanisms.** Built-in scheduling, routing, and caching,
  each sufficient to operate a fleet and each replaceable through composition.
  Support for standard autoscalers and alternative placement policies.
- **Deployment organization and controls.** Multiple model deployments on one
  fleet, organized by Kubernetes namespace, with placement priority, preemption,
  and quota ceilings.
- **Observable state and usage.** Resource status, fleet-wide metrics, and
  per-request usage records available to external controllers, consoles, and
  operational systems.
- **Stable API evolution.** Versioning, compatibility, and deprecation following
  Kubernetes conventions.

## What Modelplane Is Not

Each of the following capabilities falls outside the scope of Modelplane. These
boundaries keep the project focused on operating an inference fleet, prevent its
APIs from becoming coupled to higher-level concerns, and leave room for others
to build more opinionated systems on its open interfaces.

- **Not an optimization engine.** Modelplane executes declared policies through
  scheduling, routing, and caching. Optimizing cost, performance, and capacity
  requires objectives and tradeoffs that vary by organization. Optimizers belong
  in external controllers using Modelplane's APIs and telemetry.

- **Not multi-fleet.** Modelplane governs one fleet spanning any number of
  clusters, regions, clouds, and providers. Coordinating independently managed
  fleets introduces a separate layer of authority and lifecycle management.
  Federation and cross-fleet orchestration belong above the fleet control plane.

- **Not an identity or tenancy model.** Modelplane serves one tenant and defines
  no identity, entitlement, or tenancy primitives. Namespaces organize
  deployments but are not tenancy boundaries. The gateway authenticates
  operator-supplied credentials and treats them alike; durable identity and
  differentiated access belong in systems integrated through gateway extensions.

- **Not a queueing model.** Placement priority, preemption, and quota ceilings
  are in scope. Fair sharing, guaranteed capacity, borrowing, and queue-based
  admission require managing pending demand across competing workloads, a
  responsibility already addressed by systems such as Kueue.

- **Not a model catalog or registry.** Modelplane runs declared models and
  configurations. Model approval, curation, and hardware-specific validation
  vary by organization and belong in external catalogs or registries that supply
  configurations through its APIs.

- **Not a console.** Modelplane exposes declarative APIs, resource status,
  metrics, and usage records rather than defining a user interface. Consoles are
  independent consumers of those interfaces, allowing different operational
  experiences without coupling them to the control plane.

## Updating This Document

This charter defines Modelplane's mission, architectural principles, scope, and
boundaries. It guides contributors and maintainers in deciding what belongs in
the project and what belongs in the ecosystem around it.

Changes to the project's mission, principles, or scope follow the
decision-making process defined in [GOVERNANCE.md](GOVERNANCE.md).

Implementation details and supported technologies will evolve. The project's
commitment to open, declarative, composable, and infrastructure-independent
inference control remains its foundation.
