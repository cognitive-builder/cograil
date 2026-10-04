# ADR 0003: Postgres Is The Only State Store In v0.1; Temporal Is An Optional Backend Later

Status: Accepted 2026-10-04 · Date: 2026-10-03

## Context

Small clients should deploy one container and one database. Enterprise approvals can wait days and need durable timers, retries and visibility. The author has shipped Temporal in production and prefers it, which is exactly why it must not be the only option.

## Decision

`RunStore` is an interface. `PostgresRunStore` persists runs, tool calls, approvals and audit events and is the v0.1 default. A `TemporalRunStore` backend is scheduled for v0.5 behind the same interface. Temporal Cloud pay-as-you-go (no monthly minimum) makes the hosted path viable for small teams when the time comes.

## Consequences

Pause and resume are implemented explicitly against the store, so the LangGraph graph is rebuilt on resume rather than relying on in-process checkpoints. Long timers in v0.x use the scheduler, not durable workflows; this limit is documented.
