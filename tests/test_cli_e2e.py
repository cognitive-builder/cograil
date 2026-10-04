"""End to end for issue #13: `cograil run` pauses at a gate and `cograil approve` finishes the
Run, in two separate invocations that share only the Postgres named by DATABASE_URL."""

import asyncio
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cograil.cli import app
from cograil.cliexit import EXIT_AWAITING_APPROVAL
from cograil.domain import RunStatus
from cograil.store import PostgresRunStore

pytestmark = pytest.mark.e2e

DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
FIRST = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record, args: {item: "42"}}]
"""
REST = "- {text: Recorded, done: true}\n"


def test_run_then_approve_through_postgres(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    first, rest = tmp_path / "first.yaml", tmp_path / "rest.yaml"
    first.write_text(FIRST)
    rest.write_text(REST)
    runner = CliRunner()
    args = ["run", str(DEMO), "--protocol", "record_item", "--as", "alice@example.com"]
    args += ["--message", "Please record the answer"]
    started = runner.invoke(app, [*args, "--fake-script", str(first)])
    assert started.exit_code == EXIT_AWAITING_APPROVAL, started.output
    run_id = re.search(r"^run (\w+) ", started.output, re.MULTILINE)
    token = re.search(r"cograil approve (\S+) --as", started.output)
    assert run_id and token

    approved = runner.invoke(
        app,
        ["approve", token.group(1), "--as", "manager@example.com", "--fake-script", str(rest)],
    )
    assert approved.exit_code == 0, approved.output

    async def final_status() -> tuple[RunStatus, int]:
        store = PostgresRunStore.from_url(migrated_url)
        try:
            run = await store.get_run(run_id.group(1))
        finally:
            await store.dispose()
        return run.status, run.cursor

    assert asyncio.run(final_status()) == (RunStatus.completed, 2)
