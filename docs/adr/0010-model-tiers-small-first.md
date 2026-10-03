# ADR 0010: Model Tiers, Small First

Status: Proposed · Date: 2026-10-03 · Discipline: Small Language Models

## Context

Most steps are classification, extraction, redaction, citation checking or compression. Those tasks do not need a frontier model, and some of them must not leave the client's network.

## Decision

Models are addressed by tier, not by name: `small`, `standard`, `strong`. `harness.yaml` maps tiers to concrete models per provider (default: claude-haiku-4-5, claude-sonnet-5-5, claude-opus-5-5; local alternatives such as Gemma, Phi or Qwen through an Ollama or NVIDIA NIM provider). A step may request a tier with `(model: small)`. Classification, extraction, redaction, citation checks and compression default to `small`; judgment steps default to `standard`; `strong` is opt-in. Low-confidence small-tier results escalate one tier.

## Consequences

Cost per resolved run and data residency become configuration. Traces and audit events are the raw material for later distillation of a dedicated small classifier. Quality claims for small models are made per task from eval results, never in general.
