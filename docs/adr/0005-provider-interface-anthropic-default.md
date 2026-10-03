# ADR 0005: One Provider Interface, Anthropic Default, Model Chosen Per Protocol

Status: Proposed · Date: 2026-10-03

## Context

Cost per resolved run differs by task. Classification needs a small fast model; a judgment-heavy step may need a larger one. Clients also have provider constraints.

## Decision

`Provider.plan(step, context, tools)` is the only interface the runner uses. The Anthropic provider is the default; `claude-sonnet-5-5` for steps and `claude-haiku-4-5` for classification unless a Protocol or Colleague overrides. OpenAI and Ollama adapters are later work.

## Consequences

Model names are configuration. Token usage is returned on every call so cost telemetry is a first-class number. Evals run against the FakeProvider by default and the live provider on demand.
