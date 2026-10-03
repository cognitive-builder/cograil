# ADR 0013: Runtime Cost Discipline

Status: Proposed · Date: 2026-10-03 · Builds on: ADR 0007, 0008, 0010, 0012

## Context

Cograil is built for one architect running colleagues for several clients. Every model call is paid by a client's budget, so cost has to be designed in, not measured after the fact. The same strategy used to build Cograil applies to running it: cheap by default, the strongest model only where it earns its price, and every dollar traceable.

## Decision

1. **Tier ladder per step.** A decision table decides when it can (no model). Otherwise the step starts on its declared tier (small, standard or strong) and steps up one tier only when the result is low-confidence or fails twice. Each Colleague names a default tier, never a model.
2. **Per-step effort.** `harness.yaml` sets a default effort and a step may override it with `(effort: high)`. These map to the model API's effort levels. Ultracode is a Claude Code setting and is not used at runtime.
3. **Saved context.** The parts of a prompt that do not change between calls (colleague persona, protocol text, tool schemas) are sent first and marked for caching; per-run data follows. Cache read and write tokens are recorded separately.
4. **Batch for work that can wait.** Scheduled runs marked `batch: true` and live eval suites go through the provider's batch path at its lower price.
5. **Caps per client.** Each workspace has a monthly spending cap and an alert threshold. A run that would start over the cap is refused, audited and escalated.
6. **One headline number.** Cost per resolved run (total cost of runs that completed without escalation, divided by their count, per protocol) is the number reports lead with, not cost per token.
7. **Tiers are configuration.** Concrete model names live only in `harness.yaml`, so a retired model (Haiku 4.5 is listed for retirement no sooner than 2026-10-15) is a one-line change.

## Consequences

The provider interface gains cache markers, token categories and a batch path. Cost telemetry, evals and the harness loader grow small, specific acceptance criteria rather than a new subsystem. Quality claims for cheaper tiers are made per task from eval results.
