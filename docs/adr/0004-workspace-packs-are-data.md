# ADR 0004: Workspace Packs Are Data; The Runtime Is Code

Status: Proposed · Date: 2026-10-03

## Context

One architect serves several clients with different systems, audiences and risk tolerance. Client configuration must never reach the public repository, and the runtime must not fork per client.

## Decision

A Workspace is a folder: `colleagues/*.yaml`, `protocols/*.md`, `tools.yaml`, `connections.yaml`, `audiences.yaml`, `knowledge.yaml`, `knowledge/`. The public repo ships example workspaces only. Real client packs live in private repositories and are mounted or cloned at deploy time. Secrets are referenced by environment variable name inside `connections.yaml`, never by value.

## Consequences

Client-specific code is a smell; it belongs in a `python` tool inside the pack. Multi-tenancy is out of scope because one deployment serves one workspace, which keeps the security model simple.
