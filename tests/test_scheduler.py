"""Scheduled triggers (issue #32): cron in colleagues/*.yaml, a named system principal with an
explicit audience, and only Protocols that allow scheduled execution."""

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from cograil.api.app import create_app
from cograil.api.approval_mail import ApprovalMail
from cograil.api.auth_settings import auth_settings
from cograil.api.services import RegistryOpener, Services
from cograil.api.wiring import open_registry
from cograil.approval_links import ApprovalLinks
from cograil.audience import resolve_principal
from cograil.channels.slack.approver import SlackApprover
from cograil.domain import Colleague, RunStatus, Schedule
from cograil.errors import AudienceDenied, EmailDeliveryError, StoreNotConfigured, WorkspaceError
from cograil.providers import FakeProvider
from cograil.providers.base import Provider
from cograil.providers.fake import load_script
from cograil.scheduler import build_scheduler, cron_trigger, scheduled_principal
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
TWO_GATES = Path(__file__).parent / "fixtures/workspaces/two-gates"
# Step 1 looks a value up; step 2 plans a gated write, so the Run pauses there.
SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record, args: {item: "42"}}]
"""
# The two-gates pack: the gated write is split in two, so the scheduled Run pauses at
# demo.record_a and its approved resume pauses again at demo.record_b.
DOUBLE_SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record_a, args: {item: "42"}}]
- {text: Recorded A, done: true}
- tool_calls: [{tool: demo.record_b, args: {item: "42"}}]
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


def two_gate_workspace(tmp_path: Path) -> Path:
    """The two-gates pack with a Schedule, so a Run and its resume each stop at their own gate."""
    root = tmp_path / "two-gates"
    shutil.copytree(TWO_GATES, root)
    with (root / "colleagues/gatekeeper.yaml").open("a") as colleague:
        colleague.write(SCHEDULE.replace("record_item", "double_record"))
    with (root / "principals.yaml").open("a") as file:
        file.write(SYSTEM)
    protocol = root / "protocols/double_record.md"
    protocol.write_text(
        protocol.read_text().replace(
            "Scheduled execution: not allowed", "Scheduled execution: allowed"
        )
    )
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


class Outbox:
    """An EmailSender that keeps what it was given."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class Slack:
    """A Poster that keeps the direct messages it was asked to post."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(
        self, channel: str, thread_ts: str | None, text: str, blocks: Any = None
    ) -> None:
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})

    async def open_dm(self, user: str) -> str:
        return f"D-{user}"

    async def whisper(self, channel: str, thread_ts: str, user: str, text: str) -> None: ...

    async def replace(self, channel: str, ts: str, text: str) -> None: ...


def mail_to(outbox: Outbox) -> ApprovalMail:
    links = ApprovalLinks("k" * 32, "https://cograil.example.com")
    return ApprovalMail(outbox, "cograil@example.com", links)


class RefusingMail(Outbox):
    """An EmailSender that refuses every message."""

    async def send(self, message: EmailMessage) -> None:
        raise EmailDeliveryError("smtp: refused")


class OnceOutbox(Outbox):
    """An EmailSender that delivers the first message and refuses every later one."""

    def __init__(self) -> None:
        super().__init__()
        self._delivered = False

    async def send(self, message: EmailMessage) -> None:
        if self._delivered:
            raise EmailDeliveryError("smtp: refused")
        self._delivered = True
        await super().send(message)


class RefusingSlack(Slack):
    """A Poster whose direct messages Slack refuses to carry."""

    async def open_dm(self, user: str) -> str:
        raise RuntimeError("missing_scope")


class Env:
    def __init__(
        self,
        tmp_path: Path,
        *,
        allowed: bool = True,
        mail: Outbox | None = None,
        slack: Slack | None = None,
        root: Path | None = None,
        script: str = SCRIPT,
    ) -> None:
        # `allowed` shapes a made workspace; a given `root` is taken as it stands.
        self.root = root or make_workspace(tmp_path, allowed=allowed)
        if slack is not None:  # the approver, the Colleague's escalation_contact, is on Slack
            people = self.root / "principals.yaml"
            people.write_text(
                people.read_text().replace(
                    "  - id: manager@example.com\n",
                    "  - id: manager@example.com\n    slack_id: U_MANAGER\n",
                )
            )
        self.workspace = load_workspace(self.root)
        self.store = InMemoryRunStore()
        script_file = tmp_path / "script.yaml"
        script_file.write_text(script)
        self.provider = FakeProvider(load_script(script_file))
        self.services = Services(
            self.workspace, self.root, self.store,
            classifier=FakeProvider([]), provider_for=self.next_provider,
            open_registry=open_registry,
            approval_mail=mail_to(mail) if mail is not None else None,
        )  # fmt: skip
        if slack is not None:
            self.services.approver_channels.append(SlackApprover(self.workspace, slack))
        self.colleague = self.workspace.colleagues[0]
        self.schedule = self.colleague.schedules[0]

    def next_provider(self, protocol: object, colleague: object) -> Provider:
        return self.provider


async def test_a_scheduled_run_is_the_system_principals_and_stops_at_the_write_gate(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, mail=Outbox())
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


# A scheduled Run has no chat, so its approver is told on every channel that reaches them (#263)


async def test_a_scheduled_run_at_a_gate_emails_the_approver_when_mail_is_configured(
    tmp_path: Path,
) -> None:
    outbox = Outbox()
    env = Env(tmp_path, mail=outbox)
    run = await env.services.scheduled(env.colleague, env.schedule)
    (mail,) = outbox.sent
    assert mail["To"] == "manager@example.com" and "digest-bot" in mail.get_content()
    assert run.status is RunStatus.awaiting_approval
    kinds = [e.kind for e in await env.store.list_audit_events(run.id)]
    assert "approval.emailed" in kinds and "approval.undeliverable" not in kinds


async def test_a_scheduled_run_at_a_gate_sends_the_approver_a_slack_dm_when_only_slack_is_on(
    tmp_path: Path,
) -> None:
    slack = Slack()
    env = Env(tmp_path, slack=slack)
    run = await env.services.scheduled(env.colleague, env.schedule)
    (dm,) = slack.posts
    assert dm["channel"] == "D-U_MANAGER" and "digest-bot" in dm["blocks"][0]["text"]["text"]
    assert run.status is RunStatus.awaiting_approval
    events = await env.store.list_audit_events(run.id)
    (sent,) = [e for e in events if e.kind == "approval.slack_sent"]
    assert (sent.principal_id, sent.detail["approver"]) == ("digest-bot", "manager@example.com")


async def test_a_scheduled_run_whose_approver_no_channel_reaches_escalates(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    run = await env.services.scheduled(env.colleague, env.schedule)
    stored = await env.store.get_run(run.id)
    assert run.status is stored.status is RunStatus.escalated
    events = await env.store.list_audit_events(run.id)
    (undelivered,) = [e for e in events if e.kind == "approval.undeliverable"]
    assert undelivered.principal_id == "digest-bot"
    assert undelivered.detail["approver"] == "manager@example.com"
    (escalated,) = [e for e in events if e.kind == "run.escalated"]
    assert escalated.detail["reason"] == "approval_undeliverable"
    assert escalated.detail["contact"] == env.colleague.escalation_contact
    (approval,) = await env.store.list_approvals(run.id)
    assert approval.decision == "expired"  # nobody can decide it after the escalation


async def test_a_slack_refusal_with_no_mail_leaves_a_scheduled_run_undeliverable(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, slack=RefusingSlack())
    run = await env.services.scheduled(env.colleague, env.schedule)
    kinds = [e.kind for e in await env.store.list_audit_events(run.id)]
    assert "approval.slack_failed" in kinds and "approval.undeliverable" in kinds
    assert run.status is RunStatus.escalated


# Fall-through between the channels (issue #267): every channel is tried, and one reaching
# the approver is enough.


@pytest.mark.parametrize("failing", ["mail", "slack"], ids=["email-fails", "slack-fails"])
async def test_a_failed_channel_the_other_one_reaches_keeps_the_run_waiting(
    tmp_path: Path, failing: str
) -> None:
    mail = RefusingMail() if failing == "mail" else Outbox()
    slack = RefusingSlack() if failing == "slack" else Slack()
    env = Env(tmp_path, mail=mail, slack=slack)
    run = await env.services.scheduled(env.colleague, env.schedule)
    assert run.status is RunStatus.awaiting_approval
    kinds = {e.kind for e in await env.store.list_audit_events(run.id)}
    failed = "approval.email_failed" if failing == "mail" else "approval.slack_failed"
    sent = "approval.slack_sent" if failing == "mail" else "approval.emailed"
    assert {failed, sent} <= kinds  # the failure is audited; the other channel reached them
    assert "approval.undeliverable" not in kinds and "run.escalated" not in kinds


async def test_every_channel_failing_leaves_the_approval_undeliverable(tmp_path: Path) -> None:
    env = Env(tmp_path, mail=RefusingMail(), slack=RefusingSlack())
    run = await env.services.scheduled(env.colleague, env.schedule)
    assert run.status is RunStatus.escalated
    events = await env.store.list_audit_events(run.id)
    (undeliverable,) = [e for e in events if e.kind == "approval.undeliverable"]
    assert undeliverable.detail["tried"] == ["email", "slack"]  # every channel was tried
    escalated = next(e for e in events if e.kind == "run.escalated")
    assert escalated.detail["reason"] == "approval_undeliverable"


async def test_renotifying_tells_each_channel_once_including_slack(tmp_path: Path) -> None:
    outbox, slack = Outbox(), Slack()
    env = Env(tmp_path, mail=outbox, slack=slack)
    run = await env.services.scheduled(env.colleague, env.schedule)
    assert (len(outbox.sent), len(slack.posts)) == (1, 1)
    again = await env.services.notify_approvers(await env.store.get_run(run.id))
    assert again.status is RunStatus.awaiting_approval
    assert (len(outbox.sent), len(slack.posts)) == (1, 1)  # neither channel sends twice
    kinds = [e.kind for e in await env.store.list_audit_events(run.id)]
    assert kinds.count("approval.emailed") == 1 and kinds.count("approval.slack_sent") == 1


# A resumed scheduled Run that stops at a second Gate goes through decide, notify, escalate


async def test_a_resumed_scheduled_run_stopping_at_a_second_gate_escalates_when_no_one_is_told(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, mail=OnceOutbox(), root=two_gate_workspace(tmp_path), script=DOUBLE_SCRIPT)
    run = await env.services.scheduled(env.colleague, env.schedule)
    assert run.status is RunStatus.awaiting_approval  # the first gate: demo.record_a
    (first,) = await env.store.list_approvals(run.id)
    manager = resolve_principal(env.workspace, env.colleague.escalation_contact)
    outcome = await env.services.decide(first.token, manager, "approved")
    assert outcome.run.status is RunStatus.escalated  # it stopped again, and no one was told
    events = await env.store.list_audit_events(run.id)
    (undeliverable,) = [e for e in events if e.kind == "approval.undeliverable"]
    assert (undeliverable.detail["tool"], undeliverable.detail["tried"]) == (
        "demo.record_b",
        ["email"],
    )
    resumed = next(e for e in events if e.kind == "gate.resumed")
    assert (resumed.detail["tool"], resumed.detail["decided_by"]) == (
        "demo.record_a",
        env.colleague.escalation_contact,
    )
    escalated = next(e for e in events if e.kind == "run.escalated")
    assert escalated.detail["reason"] == "approval_undeliverable"
    called = [e.detail["tool"] for e in events if e.kind == "tool.called"]
    assert called == ["demo.lookup", "demo.record_a"]  # the second gate's call never ran
    by_tool = {a.tool: a for a in await env.store.list_approvals(run.id)}
    assert by_tool["demo.record_a"].spent_at is not None
    assert by_tool["demo.record_b"].decision == "expired"


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


# Cron correctness (#262): weekdays count as in cron, and one restriction of the day only

ALL_DAYS = "sun,mon,tue,wed,thu,fri,sat"


def day_of_week(cron: str) -> str:
    return str(next(f for f in cron_trigger(cron).fields if f.name == "day_of_week"))


@pytest.mark.parametrize(
    ("weekdays", "days"),
    [
        ("0-4", "sun,mon,tue,wed,thu"),
        ("0-6", ALL_DAYS),
        ("5-7", "sun,fri,sat"),
        ("7", "sun"),
        ("0", "sun"),
        ("sun-thu", "sun,mon,tue,wed,thu"),
        ("mon-sun", ALL_DAYS),
        ("1,3,5", "mon,wed,fri"),
        ("*/2", "sun,tue,thu,sat"),
        ("*", "*"),
    ],
)
def test_a_weekday_field_stands_for_the_days_it_does_in_cron(weekdays: str, days: str) -> None:
    assert day_of_week(f"0 9 * * {weekdays}") == days


def test_a_weekday_range_that_starts_at_sunday_fires_on_sunday() -> None:
    trigger = cron_trigger("0 9 * * 0-4")
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert trigger.get_next_fire_time(None, saturday) == datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("weekdays", ["8", "5-1", "x", "1-5/", "*/0"])
def test_a_weekday_that_is_not_one_is_an_invalid_cron(weekdays: str) -> None:
    with pytest.raises(ValueError, match="weekday"):
        cron_trigger(f"0 9 * * {weekdays}")


def test_a_schedule_that_restricts_the_day_of_month_and_the_weekday_does_not_load(
    tmp_path: Path,
) -> None:
    schedule = SCHEDULE.replace("0 2 * * 1-5", "0 9 1 * mon")
    with pytest.raises(WorkspaceError, match="restrict the day of month or the weekday, not both"):
        load_workspace(make_workspace(tmp_path, schedule=schedule))


@pytest.mark.parametrize("cron", ["0 9 1 * *", "0 9 * * mon", "0 9 */2 * mon", "0 9 1 * */2"])
def test_a_schedule_that_restricts_only_one_of_them_loads(cron: str) -> None:
    cron_trigger(cron)


# The app starts the scheduler, a tick reaches Services.scheduled, and shutdown drains the Run


def scheduled_app(
    tmp_path: Path,
    *,
    opener: RegistryOpener = open_registry,
    close: Callable[[], Awaitable[None]] | None = None,
    root: Path | None = None,
) -> tuple[FastAPI, InMemoryRunStore]:
    root = root or make_workspace(tmp_path)
    script = tmp_path / "script.yaml"
    script.write_text(SCRIPT)
    provider = FakeProvider(load_script(script))
    store = InMemoryRunStore()
    app = create_app(
        load_workspace(root), root, store,
        auth=auth_settings({"COGRAIL_AUTH": "dev", "COGRAIL_DEV_PRINCIPAL": "alice@example.com"}),
        classifier=FakeProvider([]), provider_for=lambda protocol, colleague: provider,
        open_registry=opener, close=close,
    )  # fmt: skip
    return app, store


def tick_now(app: FastAPI) -> None:
    """Make the started scheduler run the nightly job now, instead of at 02:00."""
    app.state.cograil_scheduler.modify_job("helper/nightly", next_run_time=datetime.now(UTC))


async def test_the_app_starts_the_scheduler_with_a_schedule_and_stops_it(tmp_path: Path) -> None:
    app, _ = scheduled_app(tmp_path)
    async with app.router.lifespan_context(app):
        scheduler = app.state.cograil_scheduler
        assert scheduler.running
        assert [job.id for job in scheduler.get_jobs()] == ["helper/nightly"]
    await asyncio.sleep(0)  # APScheduler finishes stopping on the next turn of the loop
    assert not scheduler.running


async def test_the_app_starts_no_scheduler_for_a_workspace_without_schedules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain"
    shutil.copytree(DEMO, root)
    app, _ = scheduled_app(tmp_path, root=root)
    async with app.router.lifespan_context(app):
        assert app.state.cograil_scheduler is None


async def test_a_tick_of_the_started_scheduler_runs_the_schedule_through_the_services(
    tmp_path: Path,
) -> None:
    app, store = scheduled_app(tmp_path)
    async with app.router.lifespan_context(app):
        tick_now(app)
        async with asyncio.timeout(5):
            while not await store.list_runs():
                await asyncio.sleep(0.02)
    [run] = await store.list_runs()
    assert (run.trigger.kind, run.principal_id, run.context["schedule"]) == (
        "schedule",
        "digest-bot",
        "nightly",
    )


async def test_shutdown_waits_for_a_scheduled_run_in_flight_before_the_store_closes(
    tmp_path: Path,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    statuses: list[list[RunStatus]] = []

    async def gated(*args: Any) -> Any:
        entered.set()
        await release.wait()
        return await open_registry(*args)

    async def close() -> None:
        statuses.append([run.status for run in await store.list_runs()])

    app, store = scheduled_app(tmp_path, opener=gated, close=close)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    tick_now(app)
    async with asyncio.timeout(5):
        await entered.wait()  # the Run is in flight
    leaving = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await asyncio.sleep(0.1)
    assert not leaving.done() and statuses == []  # the store is still open under the Run
    release.set()
    async with asyncio.timeout(5):
        await leaving
    assert statuses == [[RunStatus.escalated]]  # the Run ended before the store closed
