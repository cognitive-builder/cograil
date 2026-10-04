"""Monthly spending cap tests for issue #56: one per acceptance criterion (ADR 0013)."""

from datetime import UTC, datetime

import pytest

from cograil.budget import month_window
from cograil.domain import (
    BudgetSettings,
    Colleague,
    Harness,
    Price,
    Principal,
    Run,
    RunStatus,
    Trigger,
)
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, scripted
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
LAST_MONTH = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
CONTACT = "ops@example.com"
HARPER = Colleague(name="harper", role="HR", escalation_contact=CONTACT, protocols=["demo"])
PROTOCOL = parse_protocol('Protocol: demo\n1. Step "One": Do it.\n')
PRICING = {"fake-model": Price(input_per_mtok=1.0, output_per_mtok=1.0)}


def harness(monthly_usd: float | None = 1.0, alert_at: float = 0.5) -> Harness:
    budget = BudgetSettings(monthly_usd=monthly_usd, alert_at=alert_at)
    return Harness(budget=budget, pricing=PRICING)


def a_run(run_id: str, *, workspace: str = "w", cost: float = 0.0, at: datetime = NOW) -> Run:
    return Run(id=run_id, workspace=workspace, colleague="harper", protocol="demo",
               protocol_version=1, principal=Principal(id="alice@example.com"),
               trigger=Trigger(kind="chat"), cost_usd=cost, created_at=at,
               updated_at=at)  # fmt: skip


def an_execution(store: InMemoryRunStore, tokens: int, cap: Harness) -> tuple[Runner, FakeProvider]:
    """A Runner whose one model call costs `tokens` input tokens, at $1 per million."""
    provider = FakeProvider([scripted("ok", done=True, input_tokens=tokens, output_tokens=0)])
    runner = Runner(provider, ToolRegistry(store), store, HARPER, harness=cap, clock=lambda: NOW)
    return runner, provider


async def events(store: InMemoryRunStore, kind: str) -> list[dict[str, object]]:
    found = []
    for run in await store.list_runs(limit=50):
        found += [e.detail for e in await store.list_audit_events(run.id) if e.kind == kind]
    return found


async def test_a_run_that_would_start_over_the_cap_is_refused_audited_and_escalated() -> None:
    store = InMemoryRunStore()
    await store.create_run(a_run("spent", cost=1.0))  # the month's cap is used up
    await store.create_run(a_run("new"))
    runner, provider = an_execution(store, 100, harness())
    run = await runner.run("new", PROTOCOL)
    assert run.status is RunStatus.escalated
    assert provider.calls == []  # the model was never asked
    (escalated,) = await events(store, "run.escalated")
    assert escalated == {"reason": "monthly_cap_reached", "contact": CONTACT, "workspace": "w",
                         "spend_usd": 1.0, "monthly_usd": 1.0}  # fmt: skip
    audit = await store.list_audit_events("new")
    assert [e.principal_id for e in audit] == ["alice@example.com"]


async def test_the_cap_counts_only_this_workspace_and_this_month() -> None:
    store = InMemoryRunStore()
    await store.create_run(a_run("other-client", workspace="other", cost=5.0))
    await store.create_run(a_run("last-month", cost=5.0, at=LAST_MONTH))
    await store.create_run(a_run("new"))
    runner, _ = an_execution(store, 100, harness())
    assert (await runner.run("new", PROTOCOL)).status is RunStatus.completed


async def test_a_run_already_under_way_is_not_cut_off_by_the_cap() -> None:
    store = InMemoryRunStore()
    await store.create_run(a_run("busy", cost=2.0).model_copy(update={"status": RunStatus.running}))
    runner, _ = an_execution(store, 100, harness())
    assert (await runner.run("busy", PROTOCOL)).status is RunStatus.completed


async def test_one_alert_is_sent_when_the_months_spend_crosses_alert_at() -> None:
    store = InMemoryRunStore()
    for run_id in ("first", "second", "third"):
        await store.create_run(a_run(run_id))
    quiet, _ = an_execution(store, 400_000, harness())  # $0.40 of $1.00: below alert_at 0.5
    await quiet.run("first", PROTOCOL)
    assert await events(store, "budget.alerted") == []
    crossing, _ = an_execution(store, 200_000, harness())  # $0.60 in total: over $0.50
    await crossing.run("second", PROTOCOL)
    again, _ = an_execution(store, 100_000, harness())  # $0.70: still over, but already alerted
    await again.run("third", PROTOCOL)
    (alert,) = await events(store, "budget.alerted")
    assert alert["contact"] == CONTACT and alert["month"] == "2026-10"
    assert alert["spend_usd"] == pytest.approx(0.6) and alert["monthly_usd"] == 1.0


async def test_a_workspace_without_a_cap_is_never_refused_or_alerted() -> None:
    store = InMemoryRunStore()
    await store.create_run(a_run("spent", cost=100.0))
    await store.create_run(a_run("new"))
    runner, _ = an_execution(store, 100, harness(monthly_usd=None))
    assert (await runner.run("new", PROTOCOL)).status is RunStatus.completed
    assert await events(store, "budget.alerted") == []


@pytest.mark.parametrize(
    ("now", "start", "end"),
    [
        (NOW, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC),
         datetime(2027, 1, 1, tzinfo=UTC)),
    ],
)  # fmt: skip
def test_the_month_is_the_calendar_month_in_utc(now: datetime, start: datetime, end: datetime) -> None:
    assert month_window(now) == (start, end)
