# ADR 0007: Context Is Declared Per Step

Status: Proposed · Date: 2026-10-03 · Discipline: Context Engineering

## Context

A run accumulates tool outputs, retrieved passages and prior step results. Handing all of it to every step wastes tokens, invites injection from retrieved text, and makes behaviour depend on history the protocol author never intended.

## Decision

Each Step sees only what the protocol declares: an optional trailing directive `(context: steps 1, 2)` selects prior step outputs; tool schemas are included only for the step's whitelisted tools; retrieved passages and tool outputs are placed in an isolated data block with a fixed preamble that they are data, not instructions. Tool outputs above a configured size are compressed by the small model tier before entering context. Every run records a Window Ledger: tokens by source (instruction, prior steps, tools, knowledge) per step.

## Consequences

Context is a whitelist like tools are. Authors must think about what a step needs, which is a feature. The parser grows a directive syntax (see ADR 0011).
