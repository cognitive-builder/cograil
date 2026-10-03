# ADR 0009: Decisions That Matter Are Tables, Not Prompts

Status: Proposed · Date: 2026-10-03 · Discipline: Decision Models

## Context

Approval routing, eligibility and escalation tiers are business decisions with named owners. Leaving them to prose in a prompt makes them unauditable and unreviewable.

## Decision

A workspace may hold `decisions/*.yaml`: DMN-style tables with typed inputs, rules evaluated first-match (or collect, declared per table), and typed outputs. A new tool kind `decision` evaluates a table deterministically. The model's job is to extract the inputs; the table's job is to decide. Every evaluation writes an AuditEvent with the table name, version and rule id that fired.

## Consequences

Business owners review decisions as diffs to a table. Explainability is a lookup, not a model explanation. Anything a first-match table cannot express belongs in a `python` tool, not in a richer rules engine.
