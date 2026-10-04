"""CLI tests, one per acceptance criterion of issue #13 (and #113, approval tokens), on an
in-memory RunStore."""

import asyncio
import base64
import re
import secrets
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cograil.cli
from cograil.cli import app
from cograil.domain import RunStatus
from cograil.errors import ProviderError
from cograil.providers.fake import load_script
from cograil.store import InMemoryRunStore, RunStore

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
ALICE, MANAGER = "alice@example.com", "manager@example.com"

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
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", as_]
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
    assert code == cograil.cli.EXIT_AWAITING_APPROVAL
    order = ["run.started", "tool.called", "step 1 complete Look up", "gate.paused"]
    positions = [next(i for i, line in enumerate(lines) if want in line) for want in order]
    assert positions == sorted(positions)
    assert f"cograil approve {token_in(output)} --as {MANAGER}" in output


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
    assert result.exit_code == cograil.cli.EXIT_ERROR
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.awaiting_approval
    refused = [
        e for e in asyncio.run(shared_store.list_audit_events(run.id)) if e.kind == "gate.refused"
    ]
    assert len(refused) == (1 if decider.strip() else 0)  # a blank --as never reaches the runner


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
    assert result.exit_code == cograil.cli.EXIT_ESCALATED
    assert f"escalated to {MANAGER}" in result.output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.escalated


def test_approve_unknown_token_exits_with_an_error(shared_store: InMemoryRunStore) -> None:
    result = runner.invoke(app, ["approve", "nope", "--as", MANAGER])
    assert result.exit_code == cograil.cli.EXIT_ERROR
    assert "no such approval" in result.output


def test_a_failed_run_exits_with_the_failed_code(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    code, output = start(tmp_path, text="[]")  # the script runs out at the first plan
    assert code == cograil.cli.EXIT_FAILED
    assert "failed: ProviderError" in output
    (run,) = asyncio.run(shared_store.list_runs())
    assert run.status is RunStatus.failed


def test_a_principal_outside_the_audience_is_denied(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    code, output = start(tmp_path, as_="mallory@example.com")
    assert code == cograil.cli.EXIT_ERROR and "denied" in output
    assert asyncio.run(shared_store.list_runs()) == []


def test_run_refuses_a_workspace_whose_tools_are_not_built_yet(
    shared_store: InMemoryRunStore, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["run", str(EXAMPLE), "--protocol", "leave_request", "--as", ALICE,
         "--fake-script", script(tmp_path, "[]")],
    )  # fmt: skip
    assert result.exit_code == cograil.cli.EXIT_ERROR
    assert "cannot build the tools: notify.send" in result.output
    assert asyncio.run(shared_store.list_runs()) == []


def test_run_needs_a_database_and_an_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", ALICE]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" in runner.invoke(app, args).output
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake = ["--fake-script", script(tmp_path, "[]")]
    result = runner.invoke(app, [*args, *fake])
    assert result.exit_code == cograil.cli.EXIT_ERROR and "DATABASE_URL" in result.output


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
    assert result.exit_code == cograil.cli.EXIT_ERROR
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


@pytest.mark.parametrize("command", [[], ["validate"], ["run"], ["approve"]])
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
