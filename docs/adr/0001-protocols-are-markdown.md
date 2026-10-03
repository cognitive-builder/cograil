# ADR 0001: Protocols Are Markdown

Status: Proposed · Date: 2026-10-03

## Context

A colleague's procedure must be readable by the business owner who is accountable for it, versioned with the code that runs it, and executable by a model without a translation layer that drifts.

## Decision

A Protocol is a Markdown file with a header block, numbered `N. Step "Name": ...` lines, `@tool` references, and optional `Error handling:` and `Guardrails:` sections. The parser is small and strict; unknown tool references fail validation.

## Consequences

Business owners can review a diff. Git is the studio. The parser must stay small, which means no conditional syntax beyond prose; branching that needs structure belongs in a `python` tool or a later Workflow kind.
