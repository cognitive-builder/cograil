# ADR 0012: The Harness Is Versioned Configuration

Status: Proposed · Date: 2026-10-03 · Discipline: Harness Engineering

## Context

Context rules, loop bounds, model tiers, redaction and retry policy together determine behaviour as much as the protocol text does. If they are scattered across code, a behaviour change cannot be reviewed, reproduced or rolled back.

## Decision

`harness.yaml` in a workspace holds: `max_turns`, token and dollar budgets, tier-to-model mapping, compression threshold, redaction rules, retry policy and injection-defence settings. The runtime computes a harness version (content hash plus semantic version) and stamps it on every Run. Changing `harness.yaml` or any protocol requires the eval golden set to pass in CI before the change is marked released.

## Consequences

A run can be reproduced from its protocol version, harness version and inputs. The harness is the product's unit of improvement, and the weekly retrospective edits it deliberately.
