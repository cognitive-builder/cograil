# ADR 0008: Bounded Loops With An Explicit Completion Signal

Status: Proposed · Date: 2026-10-03 · Discipline: Loop Engineering

## Context

Inside a step the model may need several turns: call a tool, read the result, call another. Unbounded loops are the most common agent failure and the most expensive one.

## Decision

The inner loop of a step is bounded by `max_turns`, a token budget and a dollar budget from `harness.yaml`, overridable per step with `(turns: 4)`. A step ends only when the model emits the structured `step_complete` signal or a bound is hit. Hitting a bound raises `LoopBudgetExceeded`, which escalates through the gate machinery; it never continues silently and never fails silently.

## Consequences

Cost per step has a ceiling before the first run. Evals assert turn counts as well as outcomes, so a regression in loop discipline is caught by the promotion gate.
