# ADR 0002: Gates Are Tool Metadata Enforced By The Runner

Status: Proposed · Date: 2026-10-03

## Context

If a model is told in a prompt to ask before writing, it will eventually not ask. Enterprises cannot ship that variance.

## Decision

Every Tool carries `scope` (`read` or `write`) and `confirm_before_write`. The runner pauses a Run before any write tool that requires confirmation, persists the Run, and resumes only through an approval token. The model is never consulted about whether a gate exists.

## Consequences

Safety behaviour is testable without a model: `GateRequired` and `ToolNotAllowed` are unit-tested with the FakeProvider. Protocol authors cannot bypass a gate from prose; they must change tool metadata, which is reviewed in a PR.
