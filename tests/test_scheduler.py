"""Scheduled triggers (issue #32): cron in colleagues/*.yaml, a named system principal with an
explicit audience, and only Protocols that allow scheduled execution."""

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cograil.api.services import Services
from cograil.api.wiring import open_registry
from cograil.domain import Colleague, RunStatus, Schedule
from cograil.errors import AudienceDenied, StoreNotConfigured, WorkspaceError
from cograil.providers import FakeProvider
from cograil.providers.base import Provider
from cograil.providers.fake import load_script
from cograil.scheduler import build_scheduler, scheduled_principal
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
# Step 1 looks a value up; step 2 plans a gated write, so the Run pauses there.
SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record, args: {item: "42"}}]
"""
SCHEDULE = """schedules:
  - name: nightly
    protocol: record_item
    cron: "0 2 * * 1-5"
    principal: digest-bot
    audience: staff
    message: record the nightly value
"""
SYSTEM = "  - id: digest-bot\n    kind: system\n"


def make_workspace(
    tmp_path: Path, *, schedule: str = SCHEDULE, principals: str = SYSTEM, allowed: bool = True
) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(DEMO, root)
    with (root / "colleagues/helper.yaml").open("a") as colleague:
        colleague.write(schedule)
    with (root / "principals.yaml").open("a") as file:
        file.write(principals)
    if allowed:
        protocol = root / "protocols/record_item.md"
        text = protocol.read_text().replace(
            "Scheduled execution: not allowed", "Scheduled execution: allowed"
        )
        protocol.write_text(text)
    return root


def test_a_colleague_loads_its_schedules_from_yaml(tmp_path: Path) -> None:
    workspace = load_workspace(make_workspace(tmp_path))
    [schedule] = workspace.colleagues[0].schedules
    assert (schedule.name, schedule.cron, schedule.principal) == (
        "nightly",
        "0 2 * * 1-5",
        "digest-bot",
    )


def test_the_scheduler_fires_each_schedule_on_its_cron_in_utc(tmp_path: Path) -> None:
    workspace = load_workspace(make_workspace(tmp_path))

    async def fire(colleague: Colleague, schedule: Schedule) -> None: ...

    [job] = build_scheduler(workspace, fire).get_jobs()
    assert job.id == "helper/nightly"
    friday_evening = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
    assert job.trigger.get_next_fire_time(None, friday_evening) == datetime(
        2026, 10, 12, 2, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"schedule": SCHEDULE.replace("0 2 * * 1-5", "every night")}, "invalid cron"),
        ({"schedule": SCHEDULE.replace("record_item", "other")}, "not one of the colleague's"),
        ({"allowed": False}, "does not allow scheduled execution"),
        ({"principals": "  - id: digest-bot\n"}, "not a system principal"),
        ({"principals": SYSTEM + "    groups: [staff]\n"}, "takes its groups from the audience"),
        ({"schedule": SCHEDULE.replace("staff", "nobody")}, "not in audiences.yaml"),
        ({"schedule": SCHEDULE + SCHEDULE.split("\n", 1)[1]}, "used twice"),
    ],
    ids=["bad-cron", "foreign-protocol", "no-scheduled-execution", "user-principal",
         "own-groups", "no-audience", "duplicate-name"],
)  # fmt: skip
def test_a_schedule_that_cannot_run_keeps_the_workspace_from_loading(
    tmp_path: Path, edit: dict[str, object], message: str
) -> None:
    with pytest.raises(WorkspaceError, match=message):
        load_workspace(make_workspace(tmp_path, **edit))  # type: ignore[arg-type]


def test_the_run_principal_is_the_named_system_principal_with_the_audiences_groups(
    tmp_path: Path,
) -> None:
    workspace = load_workspace(make_workspace(tmp_path))
    principal = scheduled_principal(workspace, workspace.colleagues[0].schedules[0])
    assert (principal.id, principal.kind, principal.groups) == ("digest-bot", "system", ["staff"])


class Env:
    def __init__(self, tmp_path: Path, *, allowed: bool = True) -> None:
        self.root = make_workspace(tmp_path, allowed=allowed)
        self.workspace = load_workspace(self.root)
        self.store = InMemoryRunStore()
        script = tmp_path / "script.yaml"
        script.write_text(SCRIPT)
        self.provider = FakeProvider(load_script(script))
        self.services = Services(
            self.workspace, self.root, self.store,
            classifier=FakeProvider([]), provider_for=self.next_provider,
            open_registry=open_registry,
        )  # fmt: skip
        self.colleague = self.workspace.colleagues[0]
        self.schedule = self.colleague.schedules[0]

    def next_provider(self, protocol: object, colleague: object) -> Provider:
        return self.provider


async def test_a_scheduled_run_is_the_system_principals_and_stops_at_the_write_gate(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    run = await env.services.scheduled(env.colleague, env.schedule)
    stored = await env.store.get_run(run.id)
    assert run.status is RunStatus.awaiting_approval  # the gate binds the system actor too
    assert (stored.principal_id, stored.principal.kind) == ("digest-bot", "system")
    assert (stored.trigger.kind, stored.trigger.cron) == ("schedule", "0 2 * * 1-5")
    assert stored.context["input"] == {
        "message": "record the nightly value",
        "requester": "digest-bot",
    }
    events = await env.store.list_audit_events(run.id)
    fired = next(e for e in events if e.kind == "schedule.fired")
    assert fired.principal_id == "digest-bot"
    assert fired.detail == {"schedule": "nightly", "cron": "0 2 * * 1-5", "audience": "staff"}
    assert {e.principal_id for e in events} == {"digest-bot"}


async def test_a_protocol_that_stopped_allowing_scheduled_execution_starts_no_run(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.workspace.protocols[0] = env.workspace.protocols[0].model_copy(
        update={"scheduled_allowed": False}
    )
    with pytest.raises(AudienceDenied):
        await env.services.scheduled(env.colleague, env.schedule)
    assert await env.store.list_runs() == []


async def test_a_failed_schedule_is_logged_and_does_not_stop_the_scheduler(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = load_workspace(make_workspace(tmp_path))

    async def fire(colleague: Colleague, schedule: Schedule) -> None:
        raise StoreNotConfigured("no store")

    [job] = build_scheduler(workspace, fire).get_jobs()
    with caplog.at_level(logging.INFO):
        await job.func(*job.args)
    assert "schedule.failed" in caplog.text
    assert "digest-bot" in caplog.text
