"""Decision tables and the decision Tool kind: one test per acceptance criterion of issue #47."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from cograil.cli import app
from cograil.decisions import DecisionOutcome, DecisionTable
from cograil.domain import Decision, Tool, Workspace
from cograil.errors import DecisionError, ToolArgumentError, ToolConfigError
from cograil.registry import CallContext, build_registry
from cograil.store import InMemoryRunStore
from cograil.workspace import check_workspace, load_workspace

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
ROUTING = "decide.approval_routing"

runner = CliRunner()


def table(**changes: Any) -> dict[str, Any]:
    """A small valid table as YAML data, with top-level keys replaced by changes."""
    data: dict[str, Any] = {
        "name": "tier",
        "version": 2,
        "inputs": {"days": {"type": "integer"}, "kind": {"type": "string"}},
        "outputs": {"tier": {"type": "string"}},
        "rules": [
            {"id": "long", "when": {"days": ">=10"}, "then": {"tier": "director"}},
            {"id": "special", "when": {"kind": ["unpaid", "sabbatical"]}, "then": {"tier": "hr"}},
            {"id": "exact", "when": {"days": 3}, "then": {"tier": "lead"}},
        ],
    }
    return {**data, **changes}


def rules(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


# Criterion 1: typed inputs, first-match or collect rules, typed outputs, version.


@pytest.mark.parametrize(
    ("policy", "inputs", "expected"),
    [
        ("first", {"days": 12, "kind": "unpaid"}, ["long"]),
        ("collect", {"days": 12, "kind": "unpaid"}, ["long", "special"]),
        ("first", {"days": 3, "kind": "annual"}, ["exact"]),
        ("collect", {"days": 1, "kind": "annual"}, []),
    ],
)
def test_hit_policy_first_or_collect(
    policy: str, inputs: dict[str, Any], expected: list[str]
) -> None:
    outcome = DecisionTable(Decision.model_validate(table(hit_policy=policy))).evaluate(inputs)
    assert [rule for rule, _ in outcome.matches] == expected
    assert (outcome.table, outcome.version) == ("tier", 2)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"rules": rules({"id": "a", "when": {"age": 1}, "then": {"tier": "x"}})},
            "unknown inputs",
        ),
        ({"rules": rules({"id": "a", "then": {}})}, "exactly the outputs"),
        ({"rules": rules({"id": "a", "then": {"tier": 1}})}, "output tier must be of type string"),
        (
            {"rules": rules({"id": "a", "when": {"days": "lots"}, "then": {"tier": "x"}})},
            "comparison",
        ),
        ({"rules": rules({"id": "a", "when": {"days": True}, "then": {"tier": "x"}})}, "integer"),
        (
            {"rules": rules({"id": "a", "when": {"kind": []}, "then": {"tier": "x"}})},
            "a list needs",
        ),
        ({"rules": rules(*[{"id": "a", "then": {"tier": "x"}}] * 2)}, "more than once"),
    ],
)
def test_invalid_table_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(DecisionError, match=message):
        DecisionTable(Decision.model_validate(table(**changes)))


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"days": 3}, "missing inputs"),
        ({"days": 3, "kind": "a", "role": "b"}, "unknown inputs"),
        ({"days": "3", "kind": "a"}, "days must be of type integer"),
        ({"days": True, "kind": "a"}, "days must be of type integer"),
        ({"days": 1, "kind": "annual"}, "no rule matched"),
    ],
)
def test_bad_inputs_or_no_match_raise(inputs: dict[str, Any], message: str) -> None:
    with pytest.raises(DecisionError, match=message):
        DecisionTable(Decision.model_validate(table())).evaluate(inputs)


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda data: {**data, "name": "other"}, "named 'other', not 'approval_routing'"),
        (lambda data: {**data, "hit_policy": "any"}, "invalid Decision"),
        (lambda data: {**data, "rules": [{"id": "r", "then": {}}]}, "exactly the outputs"),
        (None, "decide.approval_routing needs decisions/approval_routing.yaml"),
    ],
)
def test_workspace_check_reports_bad_or_missing_tables(
    tmp_path: Path, edit: Any, problem: str
) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    file = copy / "decisions/approval_routing.yaml"
    if edit is None:
        file.unlink()
    else:
        file.write_text(yaml.safe_dump(edit(yaml.safe_load(file.read_text()))))
    assert any(problem in found for found in check_workspace(copy).problems)


# Criteria 2 and 4: the decision kind decides; approval_routing is used by leave_request.


@pytest.mark.parametrize(
    ("days", "leave_type", "role", "rule", "tier"),
    [
        (2, "unpaid", "manager", "r1", "hr_ops"),
        (11, "annual", "manager", "r2", "skip_level"),
        (10, "annual", "manager", "r3", "director"),
        (10, "annual", "staff", "default", "manager"),
    ],
)
async def test_decision_tool_decides_deterministically(
    store: InMemoryRunStore,
    ctx: CallContext,
    days: int,
    leave_type: str,
    role: str,
    rule: str,
    tier: str,
) -> None:
    args = {"duration_days": days, "leave_type": leave_type, "requester_role": role}
    workspace = load_workspace(EXAMPLE)
    async with await build_registry(workspace, store, EXAMPLE) as registry:
        first = await registry.invoke(ROUTING, args, ctx)
        assert await registry.invoke(ROUTING, args, ctx) == first
    assert first["rule"] == rule and first["outputs"]["approver_tier"] == tier
    assert (await store.list_tool_calls("r1"))[0].result == first


def test_leave_request_routes_with_the_table() -> None:
    workspace = load_workspace(EXAMPLE)
    protocol = next(p for p in workspace.protocols if p.name == "leave_request")
    assert ROUTING in protocol.steps[2].tools
    assert [d.name for d in workspace.decisions] == ["approval_routing"]


async def test_decision_tool_without_its_table_is_not_built(store: InMemoryRunStore) -> None:
    workspace = load_workspace(EXAMPLE).model_copy(update={"decisions": []})
    with pytest.raises(ToolConfigError, match="no decision table 'approval_routing'"):
        await build_registry(workspace, store, EXAMPLE)


# Criterion 3: every evaluation writes an AuditEvent with table name, version and rule id.


async def test_evaluation_is_audited_with_table_version_and_rule(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    args = {"duration_days": 12, "leave_type": "annual", "requester_role": "staff"}
    async with await build_registry(load_workspace(EXAMPLE), store, EXAMPLE) as registry:
        await registry.invoke(ROUTING, args, ctx)
    events = [e for e in await store.list_audit_events("r1") if e.kind == "decision.evaluated"]
    assert [(e.principal_id, e.detail) for e in events] == [
        (
            "alice@example.com",
            {"tool": ROUTING, "step": 2, "table": "approval_routing", "version": 1,
             "hit_policy": "first", "rules": ["r2"]},
        )
    ]  # fmt: skip


async def test_failed_evaluation_records_the_error_and_no_decision(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    tool = Tool(name="decide.tier", kind="decision", scope="read")
    workspace = Workspace(name="ws", colleagues=[], protocols=[], tools=[tool],
                          decisions=[Decision.model_validate(table())])  # fmt: skip
    async with await build_registry(workspace, store) as registry:
        with pytest.raises(DecisionError, match="no rule matched"):
            await registry.invoke("decide.tier", {"days": 1, "kind": "annual"}, ctx)
    assert "no rule matched" in str((await store.list_tool_calls("r1"))[0].error)
    kinds = [event.kind for event in await store.list_audit_events("r1")]
    assert "tool.called" in kinds and "decision.evaluated" not in kinds


# Criterion 5: `cograil decide <table> --input k=v` for manual testing.


@pytest.mark.parametrize(
    ("inputs", "code", "output"),
    [
        (["duration_days=12", "leave_type=annual", "requester_role=staff"], 0,
         'rule r2: {"approver_tier": "skip_level", "requires_hr": true}'),
        (["duration_days=twelve", "leave_type=annual", "requester_role=staff"], 1,
         "'twelve' is not of type integer"),
        (["duration_days=12"], 1, "missing inputs"),
        (["days=12"], 1, "expected name=value"),
    ],
)  # fmt: skip
def test_decide_command(inputs: list[str], code: int, output: str) -> None:
    flags = [arg for value in inputs for arg in ("--input", value)]
    result = runner.invoke(app, ["decide", "approval_routing", "--workspace", str(EXAMPLE), *flags])
    assert result.exit_code == code
    assert output in result.output


# Issue #121: an outcome the table never produced still reports a typed error, and the
# args_schema check runs before the table's own input check.


def test_first_policy_outcome_without_matches_is_a_decision_error() -> None:
    outcome = DecisionOutcome("tier", 2, "first", ())
    with pytest.raises(DecisionError, match="tier v2: no rule matched"):
        outcome.as_result()


async def test_args_schema_is_checked_before_the_table_inputs(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    args = {"duration_days": 12, "leave_type": "annual"}  # requester_role: required by both the
    # schema and the table, absent from the args, so only the order decides which error surfaces
    async with await build_registry(load_workspace(EXAMPLE), store, EXAMPLE) as registry:
        with pytest.raises(ToolArgumentError, match="requester_role"):
            await registry.invoke(ROUTING, args, ctx)
    assert "missing inputs" not in str((await store.list_tool_calls("r1"))[0].error)
