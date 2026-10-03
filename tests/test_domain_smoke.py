"""Smoke test so CI is green on the first commit. Issue 1 replaces this with full coverage."""

from datetime import UTC, datetime

from cograil.domain import Principal, Run, RunStatus, Tool, Trigger


def test_tool_defaults_to_gated_writes() -> None:
    tool = Tool(name="hris.submit_leave", kind="python", scope="write")
    assert tool.confirm_before_write is True


def test_run_round_trips() -> None:
    now = datetime.now(UTC)
    run = Run(
        id="r1",
        workspace="example-smb",
        colleague="harper",
        protocol="leave_request",
        protocol_version=1,
        principal=Principal(id="alice@example.com", groups=["all-employees"]),
        trigger=Trigger(kind="chat", channel="web"),
        created_at=now,
        updated_at=now,
    )
    assert Run.model_validate(run.model_dump()) == run
    assert run.status is RunStatus.received
