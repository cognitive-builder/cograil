"""Domain model: the source of truth for every name used in Cograil.

Vocabulary: Workspace, Colleague, Protocol, Step, Tool, Connection, Audience,
Trigger, Run, Gate, Approval, AuditEvent, KnowledgeSource, Chunk, Decision, Harness, Tier.
The shapes below are the contract; Product Plan section 3 holds the ERD.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Connection(Entity):
    """How a Tool authenticates. Secrets are referenced by env var name, never stored.

    For oauth_client_credentials, secret_env names `client_id` and `client_secret`.
    """

    name: str
    auth: Literal["none", "api_key", "oauth_client_credentials", "oidc_on_behalf_of"]
    base_url: str | None = None
    secret_env: dict[str, str] = Field(default_factory=dict)
    token_url: str | None = None
    scopes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_oauth(self) -> Connection:
        if self.auth == "oauth_client_credentials":
            missing = {"client_id", "client_secret"} - self.secret_env.keys()
            if self.token_url is None or missing:
                raise ValueError(
                    "oauth_client_credentials needs token_url and secret_env "
                    "entries for client_id and client_secret"
                )
        return self


class RestPagination(Entity):
    """Follow pages by a field in the JSON body: a next URL, or a cursor sent as a query param."""

    items: str
    next: str
    cursor_param: str | None = None
    max_pages: int = Field(default=10, ge=1)


class RestEndpoint(Entity):
    """A REST call. {placeholders} in path come from args; other args go to query or body."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    pagination: RestPagination | None = None
    max_attempts: int = Field(default=3, ge=1, le=10)


class McpServer(Entity):
    """An MCP server whose tools are registered as `<Tool.name>.<server tool name>`.

    Every server tool is scope=write unless read_tools names it: the entry's scope is not
    inherited, so a server that also exposes writes is never under-gated.
    """

    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    read_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_transport(self) -> McpServer:
        if self.transport == "stdio" and not self.command:
            raise ValueError("an stdio MCP server needs a command")
        if self.transport == "http" and not self.url:
            raise ValueError("an http MCP server needs a url")
        return self


class Tool(Entity):
    name: str
    kind: Literal["python", "rest", "mcp", "knowledge", "directory", "decision"]
    scope: Literal["read", "write"]
    description: str = ""
    args_schema: dict[str, Any] = Field(default_factory=dict)
    connection: str | None = None
    confirm_before_write: bool = True
    rest: RestEndpoint | None = None
    mcp: McpServer | None = None

    @model_validator(mode="after")
    def _check_kind_config(self) -> Tool:
        if (self.kind == "rest") != (self.rest is not None):
            raise ValueError("a rest block is required for kind rest and allowed only there")
        if (self.kind == "mcp") != (self.mcp is not None):
            raise ValueError("an mcp block is required for kind mcp and allowed only there")
        if self.kind == "rest" and self.connection is None:
            raise ValueError("a rest tool needs a connection for its base_url")
        return self


class Step(Entity):
    number: int
    name: str
    instruction: str
    tools: list[str] = Field(default_factory=list)
    context_steps: list[int] | None = None  # None means harness default (previous step only)
    model_tier: Literal["small", "standard", "strong"] | None = None
    max_turns: int | None = None


class FailureThreshold(Entity):
    """An Error handling bullet such as `@hris.get_balance fails: retry once, then escalate`.

    The runner escalates the Run once the Tool has failed max_failures times in it.
    """

    tool: str
    max_failures: int = Field(ge=1)
    rule: str


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
    failure_thresholds: list[FailureThreshold] = Field(default_factory=list)
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


class Chunk(Entity):
    """A slice of a KnowledgeSource; acl_groups is what retrieval pre-filters on."""

    id: str
    source: str
    source_uri: str
    text: str
    acl_groups: list[str]


class DecisionField(Entity):
    type: Literal["string", "integer", "number", "boolean"]


class DecisionRule(Entity):
    """`when` maps inputs to conditions (omitted inputs match anything); `then` sets outputs."""

    id: str
    when: dict[str, Any] = Field(default_factory=dict)
    then: dict[str, Any]


class Decision(Entity):
    """A decision table, decisions/<name>.yaml (ADR 0009); decisions.py checks and evaluates it."""

    name: str
    version: int = Field(ge=1)
    hit_policy: Literal["first", "collect"] = "first"
    inputs: dict[str, DecisionField]
    outputs: dict[str, DecisionField]
    rules: list[DecisionRule] = Field(min_length=1)


Tier = Literal["small", "standard", "strong"]


class LoopBounds(Entity):
    """Bounds on a Step's inner loop (ADR 0008); a Step's `(turns: N)` overrides max_turns."""

    max_turns: int = Field(default=6, ge=1)
    token_budget_per_step: int | None = Field(default=None, ge=1)
    usd_budget_per_run: float | None = Field(default=None, gt=0)


class Tiers(Entity):
    """Tier-to-model mapping (ADR 0010); concrete model names belong in harness.yaml."""

    small: str = "claude-haiku-4-5"
    standard: str = "claude-sonnet-5-5"
    strong: str = "claude-opus-5-5"


class ContextSettings(Entity):
    default_prior_steps: int = Field(default=1, ge=0)
    compression_threshold_tokens: int = Field(default=2000, ge=1)


class TierDefaults(Entity):
    classification_tier: Tier = "small"
    judgment_tier: Tier = "standard"


class RetryPolicy(Entity):
    tool_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0, ge=0)


class ApprovalSettings(Entity):
    timeout_hours: float = Field(default=72, gt=0)


class Price(Entity):
    """What one model costs, in USD per million tokens."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class Harness(Entity):
    """harness.yaml: versioned configuration stamped on every Run (ADR 0012).

    `version` is the file's semantic version; harness.py adds the content hash.
    """

    version: str = Field(default="0.0.0", pattern=r"^\d+\.\d+\.\d+$")
    loop: LoopBounds = Field(default_factory=LoopBounds)
    tiers: Tiers = Field(default_factory=Tiers)
    context: ContextSettings = Field(default_factory=ContextSettings)
    defaults: TierDefaults = Field(default_factory=TierDefaults)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)
    pricing: dict[str, Price] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_pricing(self) -> Harness:
        if self.loop.usd_budget_per_run is not None:
            tiers = {self.tiers.small, self.tiers.standard, self.tiers.strong}
            if unpriced := sorted(tiers - self.pricing.keys()):
                raise ValueError(f"usd_budget_per_run needs a price for tier models {unpriced}")
        return self


class Workspace(Entity):
    name: str
    colleagues: list[Colleague]
    protocols: list[Protocol]
    tools: list[Tool]
    connections: list[Connection] = Field(default_factory=list)
    audiences: list[Audience] = Field(default_factory=list)
    knowledge: list[KnowledgeSource] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    principals: list[Principal] = Field(default_factory=list)
    harness: Harness = Field(default_factory=Harness)


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


type TriggerKind = Literal["chat", "schedule", "webhook"]


class Trigger(Entity):
    kind: TriggerKind
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
    """Authorises one call of a gated Tool; spent_at is set when that call is let through."""

    token: str
    run_id: str
    step: int
    tool: str
    args: dict[str, Any]
    approver: str
    decision: Literal["pending", "approved", "declined", "expired"] = "pending"
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    spent_at: datetime | None = None


class AuditEvent(Entity):
    run_id: str
    at: datetime
    principal_id: str
    kind: Literal[
        "run.started",
        "tool.started",
        "tool.called",
        "decision.evaluated",
        "gate.paused",
        "gate.resumed",
        "gate.spent",
        "gate.refused",
        "tier.escalated",
        "loop.bounded",
        "run.escalated",
        "run.completed",
        "run.failed",
    ]
    detail: dict[str, Any] = Field(default_factory=dict)


class Run(Entity):
    """One execution of a Protocol. principal_id and trigger_kind mirror the objects."""

    id: str
    workspace: str
    colleague: str
    protocol: str
    protocol_version: int
    harness_version: str = "unversioned"
    principal: Principal
    principal_id: str
    trigger: Trigger
    trigger_kind: TriggerKind
    status: RunStatus = RunStatus.received
    cursor: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _mirror_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        principal, trigger = data.get("principal"), data.get("trigger")
        if principal is not None:
            pid = principal.id if isinstance(principal, Principal) else principal.get("id")
            if pid is not None:
                data.setdefault("principal_id", pid)
        if trigger is not None:
            kind = trigger.kind if isinstance(trigger, Trigger) else trigger.get("kind")
            if kind is not None:
                data.setdefault("trigger_kind", kind)
        return data

    @model_validator(mode="after")
    def _check_mirrored(self) -> Run:
        if self.principal_id != self.principal.id or self.trigger_kind != self.trigger.kind:
            raise ValueError("principal_id and trigger_kind must match principal and trigger")
        return self
