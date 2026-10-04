"""Cost telemetry tests for issue #29: one per acceptance criterion (ADR 0013)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from cograil.cost import RunUsage, charge, cost_by_protocol, format_cost_table, run_usage
from cograil.domain import (
    Colleague,
    ContextSettings,
    Harness,
    Price,
    Principal,
    Run,
    RunStatus,
    Tool,
    Trigger,
)
from cograil.harness import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.providers.base import Usage
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


async def test_compression_and_screen_calls_are_charged_to_the_run() -> None:
    # The two call sites besides a Step's turn (issue #229): the screen's and the compression's
    # tokens reach the Run's tally, and their cache tokens its dollars. The Steps' own turns
    # carry no tokens here, so every number below belongs to one of the two calls.
    raw = "balance: 12 days. " + "history line. " * 200
    store = InMemoryRunStore()
    await store.create_run(a_run().model_copy(update={"id": "r1", "protocol": "d"}))
    registry = ToolRegistry(store)

    async def invoke(args: dict[str, Any]) -> str:
        return raw

    registry.register(Tool(name="look.up", kind="python", scope="read"), invoke)
    protocol = parse_protocol(
        "Protocol: d\n"
        '1. Step "Look": Use @look.up to read the balance.\n'
        '2. Step "Tell": Tell the requester. (context: steps 1)\n'
    )
    provider = FakeProvider(
        [
            scripted(
                "",
                PlannedToolCall(id="c1", tool="look.up", args={}),
                input_tokens=0,
                output_tokens=0,
            ),
            scripted(
                "The balance is 12 days.",
                input_tokens=0,
                output_tokens=1_000_000,
                cache_write_tokens=1_000_000,
            ),  # the compression's call
            scripted("12 days", done=True, input_tokens=0, output_tokens=0),
            scripted("told", done=True, input_tokens=0, output_tokens=0),
        ],
        screens=[
            scripted("none", input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
        ],
    )
    harness = Harness(
        context=ContextSettings(compression_threshold_tokens=100),
        pricing={"fake-model": Price(input_per_mtok=1.0, output_per_mtok=2.0)},
    )
    colleague = Colleague(name="c", role="r", escalation_contact="x@example.com", protocols=["d"])
    run = await Runner(provider, registry, store, colleague, harness=harness).run("r1", protocol)
    assert run_usage(run) == RunUsage(
        input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )  # fmt: skip
    screen = 1_000_000 + 1_000_000 * CACHE_READ_FACTOR  # its fresh input, and a cache read
    compression = 1_000_000 * 2.0 + 1_000_000 * CACHE_WRITE_FACTOR  # its output, and a cache write
    assert run.cost_usd == pytest.approx((screen + compression) / 1_000_000)
