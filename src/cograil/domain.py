"""Domain model: the source of truth for every name used in Cograil.

Vocabulary: Workspace, Colleague, Protocol, Step, Tool, Connection, Audience,
Trigger, Run, Gate, Approval, AuditEvent, KnowledgeSource, Chunk.
Issue 1 completes this module; the shapes below are the contract.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Connection(Entity):
    """How a Tool authenticates. Secrets are referenced by environment variable name, never stored."""

    name: str
    auth: Literal["none", "api_key", "oauth_client_credentials", "oidc_on_behalf_of"]
    base_url: str | None = None
    secret_env: dict[str, str] = Field(default_factory=dict)


class Tool(Entity):
    name: str
    kind: Literal["python", "rest", "mcp", "knowledge", "directory", "decision"]
    scope: Literal["read", "write"]
    description: str = ""
    args_schema: dict[str, Any] = Field(default_factory=dict)
    connection: str | None = None
    confirm_before_write: bool = True


class Step(Entity):
    number: int
    name: str
    instruction: str
    tools: list[str] = Field(default_factory=list)
    context_steps: list[int] | None = None  # None means harness default (previous step only)
    model_tier: Literal["small", "standard", "strong"] | None = None
    max_turns: int | None = None


class Protocol(Entity):
    name: str
    version: int = 1
    description: str = ""
    steps: list[Step]
    helpers: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=lambda: ["everyone"])
    manual_allowed: bool = True
    scheduled_allowed: bool = False
    error_handling: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    model: str | None = None


class Colleague(Entity):
    name: str
    role: str
    escalation_contact: str
    protocols: list[str]
    model_policy: str = "claude-sonnet-5-5"
    audiences: list[str] = Field(default_factory=lambda: ["everyone"])


class Audience(Entity):
    name: str
    groups: list[str]


class KnowledgeSource(Entity):
    name: str
    path: str
    acl_groups: list[str]
    chunk_size: int = 800
    chunk_overlap: int = 120


class Workspace(Entity):
    name: str
    colleagues: list[Colleague]
    protocols: list[Protocol]
    tools: list[Tool]
    connections: list[Connection] = Field(default_factory=list)
    audiences: list[Audience] = Field(default_factory=list)
    knowledge: list[KnowledgeSource] = Field(default_factory=list)


class RunStatus(StrEnum):
    received = "received"
    planned = "planned"
    running = "running"
    awaiting_approval = "awaiting_approval"
    escalated = "escalated"
    completed = "completed"
    failed = "failed"


class Principal(Entity):
    id: str
    groups: list[str] = Field(default_factory=list)
    kind: Literal["user", "system"] = "user"


class Trigger(Entity):
    kind: Literal["chat", "schedule", "webhook"]
    channel: str | None = None
    cron: str | None = None


class ToolCall(Entity):
    step: int
    tool: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None
    started_at: datetime
    ended_at: datetime | None = None


class Approval(Entity):
    token: str
    run_id: str
    step: int
    tool: str
    args: dict[str, Any]
    approver: str
    decision: Literal["pending", "approved", "declined", "expired"] = "pending"
    decided_at: datetime | None = None


class AuditEvent(Entity):
    run_id: str
    at: datetime
    principal_id: str
    kind: Literal[
        "run.started", "tool.called", "decision.evaluated", "gate.paused", "gate.resumed",
        "tier.escalated", "loop.bounded", "run.escalated", "run.completed", "run.failed",
    ]
    detail: dict[str, Any] = Field(default_factory=dict)


class Run(Entity):
    id: str
    workspace: str
    colleague: str
    protocol: str
    protocol_version: int
    harness_version: str = "unversioned"
    principal: Principal
    trigger: Trigger
    status: RunStatus = RunStatus.received
    cursor: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime
