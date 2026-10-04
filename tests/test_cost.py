"""Cost telemetry tests for issue #29: one per acceptance criterion (ADR 0013).

Issue #221: redactions are charged to their Run, and a Run that fails or escalates on a loop
bound keeps the spend of the calls before it. The routing's charge is tested in test_api.py."""

from datetime import UTC, datetime
from typing import Any

import pytest

from cograil.cost import RunUsage, charge, cost_by_protocol, format_cost_table, run_usage
from cograil.domain import (
    Colleague,
    Harness,
    LoopBounds,
    Price,
    Principal,
    Run,
    RunStatus,
    Tool,
    Trigger,
)
from cograil.errors import ToolExecutionError
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.providers.base import Usage
from cograil.redaction import REDACT_TOOL, Redactor
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HARNESS = Harness(pricing={"m": Price(input_per_mtok=1.0, output_per_mtok=2.0)})


def a_run(protocol: str = "leave", status: RunStatus = RunStatus.running, **fields: Any) -> Run:
    return Run(
        id=f"r-{protocol}-{status}-{fields.get('cost_usd', 0)}", workspace="w", colleague="c",
        protocol=protocol, protocol_version=1, principal=Principal(id="a@example.com"),
        trigger=Trigger(kind="chat"), status=status, created_at=T0, updated_at=T0, **fields,
    )  # fmt: skip


def test_tokens_and_dollars_of_each_call_are_summed_per_run() -> None:
    run = a_run()
    run = charge(HARNESS, run, "m", Usage(input_tokens=1_000_000, output_tokens=500_000))
    run = charge(HARNESS, run, "m", Usage(input_tokens=2_000_000, output_tokens=0))
    assert run.cost_usd == pytest.approx(1.0 + 1.0 + 2.0)
    assert run_usage(run) == RunUsage(input_tokens=3_000_000, output_tokens=500_000)


def test_cached_and_batch_tokens_are_kept_apart_from_fresh_tokens() -> None:
    run = charge(
        HARNESS,
        a_run(),
        "m",
        Usage(input_tokens=10, output_tokens=5, cache_read_tokens=900, cache_write_tokens=100),
    )
    run = charge(HARNESS, run, "m", Usage(input_tokens=7, output_tokens=3, batch=True))
    assert run_usage(run) == RunUsage(
        input_tokens=10, output_tokens=5, cache_read_tokens=900, cache_write_tokens=100,
        batch_tokens=10,
    )  # fmt: skip


def test_a_run_that_made_no_call_has_an_empty_tally() -> None:
    assert run_usage(a_run()) == RunUsage()


def test_cost_per_resolved_run_counts_only_completed_runs() -> None:
    runs = [
        a_run("leave", RunStatus.completed, cost_usd=0.10),
        a_run("leave", RunStatus.completed, cost_usd=0.30),
        a_run("leave", RunStatus.escalated, cost_usd=0.60),  # spent, but never resolved
        a_run("expense", RunStatus.escalated, cost_usd=0.50),
    ]
    leave, expense = cost_by_protocol(runs)
    assert (leave.runs, leave.resolved) == (3, 2)
    assert leave.cost_usd == pytest.approx(1.0)
    assert leave.cost_per_resolved_run == pytest.approx(0.20)
    assert (expense.resolved, expense.cost_per_resolved_run) == (0, None)


def test_the_cost_table_has_a_row_per_protocol_with_token_categories() -> None:
    run = charge(HARNESS, a_run("leave", RunStatus.completed), "m",
                 Usage(input_tokens=11, output_tokens=22, cache_read_tokens=33,
                       cache_write_tokens=44))  # fmt: skip
    header, row = format_cost_table(cost_by_protocol([run]))
    for column in ("fresh_in", "fresh_out", "cache_read", "cache_write", "batch", "cost/resolved"):
        assert column in header
    assert row.split()[:8] == ["leave", "1", "1", "11", "22", "33", "44", "0"]
    assert format_cost_table([]) == ["  (no Runs)"]


async def test_the_runner_charges_every_turn_to_the_run() -> None:
    store = InMemoryRunStore()
    await store.create_run(a_run().model_copy(update={"id": "r1", "protocol": "d"}))
    colleague = Colleague(name="c", role="r", escalation_contact="x@example.com", protocols=["d"])
    protocol = parse_protocol('Protocol: d\n1. Step "One": Do it.\n')
    provider = FakeProvider([scripted("ok", done=True, input_tokens=100, output_tokens=40,
                                      cache_read_tokens=500)])  # fmt: skip
    harness = Harness(pricing={"fake-model": Price(input_per_mtok=1.0, output_per_mtok=1.0)})
    runner = Runner(provider, ToolRegistry(store), store, colleague, harness=harness)
    run = await runner.run("r1", protocol)
    assert run_usage(run) == RunUsage(input_tokens=100, output_tokens=40, cache_read_tokens=500)
    assert run.cost_usd == pytest.approx((100 + 500 * 0.1 + 40) / 1_000_000)


# Issue #221. The Runs' model is fake-model at $1 per million tokens in and out, so a Run's
# dollars are its tokens / 1e6. A redaction (the Redactor's own FakeProvider) spends 1000 + 100.

PER_TOKEN = 1 / 1_000_000
ONE_DOLLAR = Price(input_per_mtok=1.0, output_per_mtok=1.0)
PRICED = Harness(pricing={"fake-model": ONE_DOLLAR})
HARPER = Colleague(name="harper", role="HR", escalation_contact="hr@example.com",
                   protocols=["leave_request"])  # fmt: skip
LOOK_UP = scripted("", PlannedToolCall(id="c", tool="hris.get_balance", args={}))
RETRIED = parse_protocol(
    'Protocol: leave_request\n1. Step "Look up": Use @hris.get_balance.\n\nError handling:\n'
    "- @hris.get_balance fails: retry once, then escalate to the Human Manager.\n"
)


def redactions(count: int, input_tokens: int = 1000) -> FakeProvider:
    answer = PlannedToolCall(id="r", tool=REDACT_TOOL, args={"text": "[REDACTED:error]"})
    plan = scripted("", answer, input_tokens=input_tokens, output_tokens=100)
    return FakeProvider([plan] * count)


async def _fail(args: dict[str, Any]) -> None:
    raise RuntimeError("no record for bob@example.com")


def failing_registry(store: InMemoryRunStore, redactor: FakeProvider) -> ToolRegistry:
    registry = ToolRegistry(store, Redactor(redactor))
    registry.register(Tool(name="hris.get_balance", kind="python", scope="read"), _fail)
    return registry


async def test_a_runs_redaction_calls_are_charged_to_it(store: InMemoryRunStore) -> None:
    # Each failure is redacted for the ToolCall, then for the Step's record or the escalation.
    redactor = redactions(4)
    registry = failing_registry(store, redactor)
    runner = Runner(FakeProvider([LOOK_UP, LOOK_UP]), registry, store, HARPER, harness=PRICED)
    run = await runner.run("r1", RETRIED)
    assert run.status is RunStatus.escalated and len(redactor.calls) == 4
    expected = RunUsage(input_tokens=2 * 10 + 4 * 1000, output_tokens=2 * 5 + 4 * 100)
    stored = await store.get_run("r1")
    assert run_usage(stored) == expected
    assert stored.cost_usd == pytest.approx(expected.total_tokens * PER_TOKEN)


async def test_a_run_that_fails_keeps_the_spend_of_its_calls(store: InMemoryRunStore) -> None:
    # Two turns, then a Tool with no failure threshold fails: its error is redacted for the
    # ToolCall and again for the run.failed AuditEvent.
    redactor = redactions(2)
    protocol = parse_protocol('Protocol: leave_request\n1. Step "Look up": Use @hris.get_balance.')
    runner = Runner(FakeProvider([scripted("thinking"), LOOK_UP]),
                    failing_registry(store, redactor), store, HARPER, harness=PRICED)  # fmt: skip
    with pytest.raises(ToolExecutionError):
        await runner.run("r1", protocol)
    stored = await store.get_run("r1")
    expected = RunUsage(input_tokens=2 * 10 + 2 * 1000, output_tokens=2 * 5 + 2 * 100)
    assert stored.status is RunStatus.failed and len(redactor.calls) == 2
    assert run_usage(stored) == expected
    assert stored.cost_usd == pytest.approx(expected.total_tokens * PER_TOKEN)


async def test_a_run_that_escalates_on_a_loop_bound_keeps_the_call_that_breached_it(
    store: InMemoryRunStore,
) -> None:
    # The small tier's unsure answer spends the Step's 15 tokens; asking one tier up is refused.
    harness = PRICED.model_copy(update={"loop": LoopBounds(token_budget_per_step=15)})
    protocol = parse_protocol('Protocol: leave_request\n1. Step "A": Answer. (model: small)')
    provider = FakeProvider([scripted("unsure", done=True, confidence=0.2)])
    run = await Runner(provider, ToolRegistry(store), store, HARPER, harness=harness).run(
        "r1", protocol
    )
    assert run.status is RunStatus.escalated
    stored = await store.get_run("r1")
    assert run_usage(stored) == RunUsage(input_tokens=10, output_tokens=5)
    assert stored.cost_usd == pytest.approx(15 * PER_TOKEN)


async def test_the_dollar_budget_counts_a_runs_redactions(store: InMemoryRunStore) -> None:
    # One failure is redacted twice at $1 each; the $1.50 budget stops the second turn.
    tiers = ("claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5", "fake-model")
    harness = Harness(pricing=dict.fromkeys(tiers, ONE_DOLLAR),
                      loop=LoopBounds(usd_budget_per_run=1.5))  # fmt: skip
    registry = failing_registry(store, redactions(2, input_tokens=1_000_000 - 100))
    provider = FakeProvider([LOOK_UP, LOOK_UP])
    run = await Runner(provider, registry, store, HARPER, harness=harness).run("r1", RETRIED)
    assert run.status is RunStatus.escalated and len(provider.calls) == 1
    [bounded] = [e for e in await store.list_audit_events("r1") if e.kind == "loop.bounded"]
    assert bounded.detail["bound"] == "usd_budget_per_run"
    assert (await store.get_run("r1")).cost_usd == pytest.approx(2.0 + 15 * PER_TOKEN)
