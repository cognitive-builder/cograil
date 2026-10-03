# ADR 0006: Static HTML Web Chat, No Frontend Framework In v0.x

Status: Proposed · Date: 2026-10-03

## Context

The web touchpoint must deploy anywhere, build in seconds in a cloud session, and stay small enough to review in a PR.

## Decision

The web chat is a single HTML file with inline CSS and JavaScript, served by FastAPI, using Server-Sent Events for streaming. No React, no build step.

## Consequences

Fewer dependencies and faster sessions. A richer admin UI, if ever needed, is a separate decision.
