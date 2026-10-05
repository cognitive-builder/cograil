"""CLI tests, one per acceptance criterion of issue #13 (and #113, approval tokens, #111
friendly errors, #110 the tool pack a Run started with), on an in-memory RunStore."""

import asyncio
import base64
import re
import secrets
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

import cograil.cli
import cograil.cliexit
from cograil.cli import app
from cograil.cost import charge
from cograil.domain import Harness, Price, Principal, Run, RunStatus, Trigger
from cograil.errors import ProviderError
from cograil.providers.base import Usage
from cograil.providers.fake import load_script
from cograil.store import InMemoryRunStore, RunStore

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
ALICE, MANAGER = "alice@example.com", "manager@example.com"
ASK = ["--message", "Please record the answer"]

# Step 1 looks a value up; step 2 plans a gated write, so the Run pauses there.
RUN_SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record, args: {item: "42"}}]
"""
REST_SCRIPT = "- {text: Recorded, done: true}\n"

runner = CliRunner()


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryRunStore]:
    """One store for every CLI call of a test, as Postgres would be across processes."""
    store = InMemoryRunStore()

    @asynccontextmanager
    async def open_store() -> AsyncIterator[RunStore]:
        yield store

    monkeypatch.setattr(cograil.cli, "open_store", open_store)
    yield store


# The CLI runs its own event loop (asyncio.run), so these tests are plain functions.


def script(tmp_path: Path, text: str, name: str = "script.yaml") -> str:
    file = tmp_path / name
    file.write_text(text)
    return str(file)


def start(tmp_path: Path, as_: str = ALICE, text: str = RUN_SCRIPT) -> "tuple[int, str]":
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", as_, *ASK]
    result = runner.invoke(app, [*args, "--fake-script", script(tmp_path, text)])
    return result.exit_code, result.output


def token_in(output: str) -> str:
    found = re.search(r"cograil approve (\S+) --as", output)
    assert found, output
    return found.group(1)


def test_run_streams_step_progress_and_stops_at_the_gate(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    code, output = start(tmp_path)
    lines = output.splitlines()
    assert code == cograil.cliexit.EXIT_AWAITING_APPROVAL
    order = ["run.started", "tool.called", "step 1 complete Look up", "gate.paused"]
    positions = [next(i for i, line in enumerate(lines) if want in line) for want in order]
    assert positions == sorted(positions)
    assert f"cograil approve {token_in(output)} --as {MANAGER}" in output


def test_run_stores_the_message_and_requester_as_the_run_input(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    start(tmp_path)
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.context["input"] == {"message": "Please record the answer", "requester": ALICE}


@pytest.mark.parametrize("ask", [[], ["--message", "  "]], ids=["missing", "blank"])
def test_run_without_a_message_is_a_usage_error(
    shared_store: InMemoryRunStore, tmp_path: Path, ask: list[str]
) -> None:
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", ALICE, *ask]
    result = runner.invoke(app, [*args, "--fake-script", script(tmp_path, RUN_SCRIPT)])
    assert result.exit_code == 2
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)  # CI colours the usage panel
    assert "--message" in plain
    assert asyncio.run(shared_store.list_runs()) == []


def test_approve_resumes_exactly_at_the_paused_step(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    _, output = start(tmp_path)
    resumed = runner.invoke(
        app,
        ["approve", token_in(output), "--as", MANAGER,
         "--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")],
    )  # fmt: skip
    assert resumed.exit_code == 0, resumed.output
    assert "gate.resumed" in resumed.output and "step 2 complete Record" in resumed.output
    assert "step 1 complete" not in resumed.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert (run.status, run.cursor) == (RunStatus.completed, 2)
    assert run.context["steps"]["2"]["tool_calls"][0]["result"] == {"recorded": "42"}


@pytest.mark.parametrize(
    "decider",
    [ALICE, "mallory@example.com", "  ", ""],
    ids=["the-requester", "someone-else", "blank", "empty"],
)
def test_approve_refuses_anyone_but_the_approver(
    shared_store: InMemoryRunStore, tmp_path: Path, decider: str
) -> None:
    _, output = start(tmp_path)
    rest = ["--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")]
    result = runner.invoke(app, ["approve", token_in(output), "--as", decider, *rest])
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.awaiting_approval
    refused = [
        e for e in asyncio.run(shared_store.list_audit_events(run.id)) if e.kind == "gate.refused"
    ]
    assert len(refused) == (1 if decider.strip() else 0)  # a blank --as never reaches the runner


# What the Run started with, changed before it is decided (issues #110, #97): a file, the text
# replaced in it (a missing file is created), and what the refusal names.
WORKSPACE_CHANGES = pytest.mark.parametrize(
    ("file", "old", "new", "says"),
    [
        ("tools.yaml", "Record an item.", "Record any item.", "tool pack"),
        ("tools/demo.py", "def record(", "AUDITED = False\n\n\ndef record(", "tool pack"),
        ("harness.yaml", "", "loop: {max_turns: 7}\n", "harness"),
    ],
    ids=["tools-yaml", "python-module", "harness-yaml"],
)


def started_then_changed(tmp_path: Path, file: str, old: str, new: str) -> str:
    """The token of a Run paused in a copy of the demo workspace, changed after the pause."""
    copy = tmp_path / "ws"
    shutil.copytree(DEMO, copy)
    args = ["run", str(copy), "--protocol", "record_item", "--as", ALICE, *ASK]
    started = runner.invoke(app, [*args, "--fake-script", script(tmp_path, RUN_SCRIPT)])
    path = copy / file
    text = path.read_text() if path.exists() else ""
    edited = text.replace(old, new, 1)
    assert edited != text
    path.write_text(edited)
    return token_in(started.output)


@WORKSPACE_CHANGES
def test_approve_refuses_a_run_whose_workspace_changed(
    shared_store: InMemoryRunStore, tmp_path: Path, file: str, old: str, new: str, says: str
) -> None:
    token = started_then_changed(tmp_path, file, old, new)
    rest = ["--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")]
    result = runner.invoke(app, ["approve", token, "--as", MANAGER, *rest])
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert "refused" in result.output and says in result.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.awaiting_approval
    (approval,) = asyncio.run(shared_store.list_approvals(run.id))
    assert approval.decision == "pending"
    assert asyncio.run(shared_store.list_tool_calls(run.id))[-1].tool == "demo.lookup"
    refused = asyncio.run(shared_store.list_audit_events(run.id))[-1]
    assert (refused.kind, refused.principal_id) == ("gate.refused", MANAGER)
    assert refused.detail["reason"] == "run_version_changed"


@WORKSPACE_CHANGES
def test_decline_escalates_a_run_whose_workspace_changed(
    shared_store: InMemoryRunStore, tmp_path: Path, file: str, old: str, new: str, says: str
) -> None:
    token = started_then_changed(tmp_path, file, old, new)
    result = runner.invoke(app, ["approve", token, "--as", MANAGER, "--decline"])
    assert result.exit_code == cograil.cliexit.EXIT_ESCALATED, result.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.escalated
    escalated = asyncio.run(shared_store.list_audit_events(run.id))[-1]
    assert (escalated.kind, escalated.detail["decided_by"]) == ("run.escalated", MANAGER)


class TakenOverAfterTheDecisionStore(InMemoryRunStore):
    """A racing execution claims the decided Run before the resume's claim lands (#104): the
    Run the decision set running is no longer this command's to go on with."""

    async def claim_run(self, run: Run, read: Run) -> None:
        if read.status is RunStatus.running:  # the read a resume claims from
            await super().claim_run(run.model_copy(update={"claim": "racing-execution"}), read)
        await super().claim_run(run, read)


def test_approve_exits_with_an_error_when_the_run_was_taken_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = TakenOverAfterTheDecisionStore()

    @asynccontextmanager
    async def open_store() -> AsyncIterator[RunStore]:
        yield store

    monkeypatch.setattr(cograil.cli, "open_store", open_store)
    _, output = start(tmp_path)
    rest = ["--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")]
    result = runner.invoke(app, ["approve", token_in(output), "--as", MANAGER, *rest])
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert "refused" in result.output
    (run,) = asyncio.run(store.list_runs())
    assert run.status is RunStatus.running  # the racing execution's to finish


def test_approve_takes_a_token_whose_random_bytes_once_made_a_leading_dash(
    shared_store: InMemoryRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b"\xf8" + bytes(15)
    assert base64.urlsafe_b64encode(raw).startswith(b"-")  # what token_urlsafe(16) gave
    monkeypatch.setattr(secrets, "token_bytes", lambda n: raw[:n])
    _, output = start(tmp_path)
    rest = ["--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")]
    result = runner.invoke(app, ["approve", token_in(output), "--as", MANAGER, *rest])
    assert result.exit_code == 0, result.output
    assert token_in(output) == raw.hex()


def test_decline_escalates_with_its_own_exit_code(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    _, output = start(tmp_path)
    result = runner.invoke(app, ["approve", token_in(output), "--as", MANAGER, "--decline"])
    assert result.exit_code == cograil.cliexit.EXIT_ESCALATED
    assert f"escalated to {MANAGER}" in result.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.escalated


def test_approve_unknown_token_exits_with_an_error(shared_store: InMemoryRunStore) -> None:
    result = runner.invoke(app, ["approve", "nope", "--as", MANAGER])
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert "no such approval" in result.output


def test_a_failed_run_exits_with_the_failed_code(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    code, output = start(tmp_path, text="[]")  # the script runs out at the first plan
    assert code == cograil.cliexit.EXIT_FAILED
    assert "failed: ProviderError" in output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.failed


def test_a_principal_outside_the_audience_is_denied(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    code, output = start(tmp_path, as_="mallory@example.com")
    assert code == cograil.cliexit.EXIT_ERROR and "denied" in output
    assert asyncio.run(shared_store.list_runs()) == []


def test_run_refuses_a_workspace_whose_tools_are_not_built_yet(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(DEMO, copy)
    tools = (copy / "tools.yaml").read_text()
    # The directory kind is the one Tool kind still without an implementation.
    unbuilt = tools.replace(
        "name: demo.lookup\n    kind: python", "name: demo.lookup\n    kind: directory", 1
    )
    assert unbuilt != tools
    (copy / "tools.yaml").write_text(unbuilt)
    result = runner.invoke(
        app,
        ["run", str(copy), "--protocol", "record_item", "--as", ALICE, *ASK,
         "--fake-script", script(tmp_path, "[]")],
    )  # fmt: skip
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert "tool demo.lookup (kind directory) is not available" in result.output
    assert asyncio.run(shared_store.list_runs()) == []


def test_run_names_the_database_url_a_knowledge_tool_needs(
    shared_store: InMemoryRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = runner.invoke(
        app,
        ["run", str(EXAMPLE), "--protocol", "policy_question", "--as", ALICE, *ASK,
         "--fake-script", script(tmp_path, "[]")],
    )  # fmt: skip
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert "knowledge.search (kind knowledge) is not available" in result.output
    assert "DATABASE_URL" in result.output
    assert asyncio.run(shared_store.list_runs()) == []


def test_the_example_workspace_builds_every_tool(
    shared_store: InMemoryRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The knowledge kind registers only when DATABASE_URL is set. A dummy URL is enough:
    # open_store is faked above, and engine creation is lazy, so nothing connects.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@localhost:5432/cograil")
    result = runner.invoke(
        app,
        ["run", str(EXAMPLE), "--protocol", "policy_question", "--as", ALICE, *ASK,
         "--fake-script", script(tmp_path, "[]")],
    )  # fmt: skip
    assert "is not available" not in result.output
    # The Run starts; it fails only because the scripted provider has no plan to give.
    assert result.exit_code == cograil.cliexit.EXIT_FAILED
    assert len(asyncio.run(shared_store.list_runs())) == 1


def test_run_needs_a_database_and_an_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", ALICE, *ASK]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" in runner.invoke(app, args).output
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake = ["--fake-script", script(tmp_path, "[]")]
    result = runner.invoke(app, [*args, *fake])
    assert result.exit_code == cograil.cliexit.EXIT_ERROR and "DATABASE_URL" in result.output


def test_an_unreachable_database_prints_one_line_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def unreachable() -> AsyncIterator[RunStore]:
        # What a dead Postgres gives: a SQLAlchemy error whose text is many lines.
        raise OperationalError("connect", {}, Exception("connection refused"))
        yield

    monkeypatch.setattr(cograil.cli, "open_store", unreachable)
    result = runner.invoke(app, ["runs"])
    (line,) = result.output.strip().splitlines()  # one line, never a traceback
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert line == "error: OperationalError: (builtins.Exception) connection refused"


def broken_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "ws"
    shutil.copytree(DEMO, copy)
    (copy / "tools.yaml").write_text("tools: [unclosed")
    (copy / "colleagues/helper.yaml").write_text("name: helper\n")
    (copy / "audiences.yaml").write_text("audiences: nope\n")
    (copy / "protocols/bad.md").write_text("Protocol: bad\n\nnot a step\n")
    return copy


def test_validate_reports_every_problem_not_just_the_first(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(broken_copy(tmp_path))])
    files = ("tools.yaml", "helper.yaml", "audiences.yaml", "bad.md")
    assert result.exit_code == cograil.cliexit.EXIT_ERROR
    assert all("invalid: " in result.output and name in result.output for name in files)
    assert "4 problem(s) found" in result.output


def test_validate_exits_zero_on_the_demo_workspace() -> None:
    result = runner.invoke(app, ["validate", str(DEMO)])
    assert result.exit_code == 0 and "ok: cli-demo" in result.output


@pytest.mark.parametrize(
    ("steps", "collides"),
    [
        ('1. Step "Both": Use @demo.lookup and @demo__lookup.\n', True),
        ('1. Step "One": Use @demo.lookup.\n2. Step "Other": Use @demo__lookup.\n', False),
    ],
    ids=["same-step", "different-steps"],
)
def test_validate_finds_wire_name_collisions_per_step(
    tmp_path: Path, steps: str, collides: bool
) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(DEMO, copy)
    with (copy / "tools.yaml").open("a") as tools:
        tools.write("  - {name: demo__lookup, kind: python, scope: read}\n")
    head = (copy / "protocols/record_item.md").read_text().split("1. Step")[0]
    (copy / "protocols/record_item.md").write_text(head + steps)
    result = runner.invoke(app, ["validate", str(copy)])
    assert (result.exit_code == 1) is collides
    assert ("share the wire name" in result.output) is collides
    if collides:
        assert "protocol record_item, step 1" in result.output


@pytest.mark.parametrize(
    "command",
    [[], ["validate"], ["run"], ["approve"], ["runs"], ["decide"], ["knowledge", "sync"]],
)
def test_exit_codes_are_documented_in_help(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0
    assert "Exit codes" in result.output
    assert "0" in result.output and "1" in result.output


@pytest.mark.parametrize(
    "text", ["not: a list", "- {text: x, extra: 1}", "- {tool_calls: [{args: {}}]}"]
)
def test_fake_script_rejects_a_malformed_plan(tmp_path: Path, text: str) -> None:
    with pytest.raises(ProviderError):
        load_script(Path(script(tmp_path, text)))


def test_runs_cost_prints_a_table_by_protocol(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    start(tmp_path)  # leaves one Run of record_item awaiting approval: spent, not resolved
    result = runner.invoke(app, ["runs", "--cost"])
    header, row = result.output.splitlines()
    assert result.exit_code == 0
    assert "cost/resolved" in header and "batch" in header
    assert row.split()[:3] == ["record_item", "1", "0"] and row.split()[-1] == "-"


T0 = datetime(2026, 10, 4, tzinfo=UTC)
PRICED = Harness(pricing={"m": Price(input_per_mtok=1.0, output_per_mtok=2.0)})


def spent(run_id: str, protocol: str, status: RunStatus, minutes: int, usage: Usage) -> Run:
    """A Run of `protocol` from `minutes` after T0, holding one priced call's tally and cost."""
    at = T0 + timedelta(minutes=minutes)
    run = Run(id=run_id, workspace="cli-demo", colleague="helper", protocol=protocol,
              protocol_version=1, principal=Principal(id=ALICE), trigger=Trigger(kind="chat"),
              status=status, created_at=at, updated_at=at)  # fmt: skip
    return charge(PRICED, run, "m", usage)


def test_cost_counts_the_newest_limit_runs_across_all_protocols(
    shared_store: InMemoryRunStore,
) -> None:
    # The window of --cost is the newest --limit Runs across all Protocols (issue #229): the
    # two oldest never reach the table, and neither Protocol gets the limit to itself.
    runs = [
        spent(
            "old-leave", "leave_request", RunStatus.escalated, 1, Usage(input_tokens=300_000)
        ),  # outside the window, never counted
        spent(
            "old-record", "record_item", RunStatus.completed, 2, Usage(input_tokens=20_000)
        ),  # outside the window, never counted
        spent(
            "new-leave-1",
            "leave_request",
            RunStatus.completed,
            3,
            Usage(input_tokens=100_000, output_tokens=50_000),
        ),
        spent(
            "new-record-1",
            "record_item",
            RunStatus.escalated,
            4,
            Usage(input_tokens=30_000, cache_read_tokens=100_000),
        ),
        spent("new-leave-2", "leave_request", RunStatus.completed, 5, Usage(input_tokens=10_000)),
        spent("new-record-2", "record_item", RunStatus.escalated, 6, Usage(input_tokens=5_000)),
    ]
    for run in runs:
        asyncio.run(shared_store.create_run(run))
    result = runner.invoke(app, ["runs", "--cost", "--limit", "4"])
    assert result.exit_code == 0, result.output
    record, leave = (line.split() for line in result.output.splitlines()[1:])
    assert record == [
        "record_item", "2", "0", "35000", "0", "100000", "0", "0", "$0.0450", "-",
    ]  # fmt: skip
    assert leave == [
        "leave_request", "2", "2", "110000", "50000", "0", "0", "0", "$0.2100", "$0.1050",
    ]  # fmt: skip


# Issue #251: a Harness with a dollar budget and no price for the fake model.
BUDGETED = """\
loop: {usd_budget_per_run: 0.50}
tiers: {small: m, standard: m, strong: m}
pricing: {m: {input_per_mtok: 1.0, output_per_mtok: 2.0}}
"""


def test_fake_script_needs_no_price_for_the_fake_model_under_a_dollar_budget(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(DEMO, copy)
    (copy / "harness.yaml").write_text(BUDGETED)
    args = ["run", str(copy), "--protocol", "record_item", "--as", ALICE, *ASK]
    started = runner.invoke(app, [*args, "--fake-script", script(tmp_path, RUN_SCRIPT)])
    assert started.exit_code == cograil.cliexit.EXIT_AWAITING_APPROVAL, started.output
    assert "loop_budget_exceeded" not in started.output
    rest = ["--fake-script", script(tmp_path, REST_SCRIPT, "rest.yaml")]
    resumed = runner.invoke(app, ["approve", token_in(started.output), "--as", MANAGER, *rest])
    assert resumed.exit_code == 0, resumed.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.completed
