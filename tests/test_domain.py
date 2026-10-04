"""Domain model tests: one per acceptance criterion of issue #5."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from cograil.domain import (
    Approval,
    Audience,
    AuditEvent,
    Chunk,
    Colleague,
    Connection,
    Entity,
    KnowledgeSource,
    McpServer,
    Principal,
    Protocol,
    Run,
    RunStatus,
    Step,
    Tool,
    ToolCall,
    Trigger,
    Workspace,
)

NOW = datetime(2026, 10, 4, tzinfo=UTC)

TOOL = Tool(name="hris.submit_leave", kind="python", scope="write")
STEP = Step(number=1, name="Collect", instruction="Ask for dates.", tools=["hris.submit_leave"])
PROTOCOL = Protocol(name="leave_request", steps=[STEP])
COLLEAGUE = Colleague(
    name="harper", role="HR", escalation_contact="hr@example.com", protocols=["leave_request"]
)
RUN = Run(
    id="r1",
    workspace="example-smb",
    colleague="harper",
    protocol="leave_request",
    protocol_version=1,
    principal=Principal(id="alice@example.com", groups=["all-employees"]),
    trigger=Trigger(kind="chat", channel="web"),
    created_at=NOW,
    updated_at=NOW,
)

ENTITIES: list[Entity] = [
    Connection(name="hris", auth="api_key", secret_env={"key": "HRIS_KEY"}),
    TOOL,
    STEP,
    PROTOCOL,
    COLLEAGUE,
    Audience(name="everyone", groups=["all-employees"]),
    KnowledgeSource(name="handbook", path="knowledge/handbook", acl_groups=["all-employees"]),
    Chunk(id="c1", source="handbook", source_uri="handbook.md#1", text="x", acl_groups=["hr"]),
    Workspace(name="example-smb", colleagues=[COLLEAGUE], protocols=[PROTOCOL], tools=[TOOL]),
    Principal(id="alice@example.com"),
    Trigger(kind="schedule", cron="0 8 * * 1"),
    ToolCall(step=1, tool="hris.submit_leave", args={"days": 2}, started_at=NOW),
    Approval(token="t1", run_id="r1", step=1, tool="hris.submit_leave", args={}, approver="bob"),
    AuditEvent(run_id="r1", at=NOW, principal_id="alice@example.com", kind="run.started"),
    RUN,
]

ERD_FIELDS: list[tuple[type[Entity], set[str]]] = [
    (Colleague, {"name", "role", "escalation_contact", "model_policy"}),
    (Protocol, {"name", "version", "manual_allowed", "scheduled_allowed"}),
    (Tool, {"kind", "scope", "confirm_before_write", "args_schema"}),
    (Run, {"status", "principal_id", "cursor", "trigger_kind"}),
    (Chunk, {"acl_groups", "source_uri"}),
]


@pytest.mark.parametrize(("model", "fields"), ERD_FIELDS)
def test_erd_fields_exist(model: type[Entity], fields: set[str]) -> None:
    assert fields <= set(model.model_fields)


def test_tool_scope_and_gate_default() -> None:
    assert TOOL.confirm_before_write is True
    with pytest.raises(ValidationError):
        Tool(name="t", kind="python", scope="admin")  # type: ignore[arg-type]


def test_run_status_values() -> None:
    assert [s.value for s in RunStatus] == [
        "received",
        "planned",
        "running",
        "awaiting_approval",
        "escalated",
        "completed",
        "failed",
    ]
    assert RUN.status is RunStatus.received


@pytest.mark.parametrize("entity", ENTITIES, ids=lambda e: type(e).__name__)
def test_round_trip(entity: Entity) -> None:
    assert type(entity).model_validate(entity.model_dump()) == entity
    assert type(entity).model_validate_json(entity.model_dump_json()) == entity


def test_extra_fields_are_forbidden() -> None:
    data: dict[str, Any] = {**TOOL.model_dump(), "surprise": 1}
    with pytest.raises(ValidationError):
        Tool.model_validate(data)


def test_run_mirrors_principal_and_trigger() -> None:
    assert (RUN.principal_id, RUN.trigger_kind) == ("alice@example.com", "chat")
    with pytest.raises(ValidationError):
        Run.model_validate({**RUN.model_dump(), "principal_id": "mallory@example.com"})


@pytest.mark.parametrize(
    ("key", "value", "loc"),
    [
        ("principal", {"groups": ["all-employees"]}, ("principal", "id")),
        ("trigger", {"channel": "web"}, ("trigger", "kind")),
    ],
    ids=["principal-without-id", "trigger-without-kind"],
)
def test_run_missing_mirror_key_names_the_field(
    key: str, value: dict[str, Any], loc: tuple[str, str]
) -> None:
    with pytest.raises(ValidationError) as exc:
        Run.model_validate({**RUN.model_dump(), key: value})
    errors = exc.value.errors()
    assert (loc, "missing") in [(e["loc"], e["type"]) for e in errors]
    assert all(e["type"] == "missing" for e in errors)


def test_trigger_kind_is_defined_once() -> None:
    assert Trigger.model_fields["kind"].annotation is Run.model_fields["trigger_kind"].annotation


ENDPOINT = {"method": "GET", "path": "/people"}
STDIO = {"transport": "stdio", "command": "calendar-mcp"}


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (Tool, {"name": "a.b", "kind": "rest", "scope": "read", "connection": "hris"}),
        (Tool, {"name": "a.b", "kind": "rest", "scope": "read", "rest": ENDPOINT}),
        (Tool, {"name": "a.b", "kind": "python", "scope": "read", "rest": ENDPOINT}),
        (Tool, {"name": "cal", "kind": "mcp", "scope": "read"}),
        (Tool, {"name": "cal", "kind": "python", "scope": "read", "mcp": STDIO}),
        (McpServer, {"transport": "stdio"}),
        (McpServer, {"transport": "http"}),
        (Connection, {"name": "hris", "auth": "oauth_client_credentials"}),
    ],
    ids=["rest-no-block", "rest-no-connection", "rest-block-on-python", "mcp-no-block",
         "mcp-block-on-python", "stdio-no-command", "http-no-url", "oauth-no-token-url"],
)  # fmt: skip
def test_tool_kind_configuration_is_validated(model: type[Entity], data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)
