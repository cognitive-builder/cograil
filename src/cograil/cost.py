"""Cost telemetry: tokens and dollars of every model call, summed per Run and per Protocol.

Every model call (a Step's turn, a compression, an injection screen) goes through `charge`,
which adds its dollars to `Run.cost_usd` and its tokens to the Run's tally in
`Run.context["usage"]`. The tally keeps fresh tokens (prompt not read from or written to saved
context, and what the model wrote), cache read and cache write tokens, and batch tokens apart,
as ADR 0013 asks: a cache read and a batch token are billed at a different rate from a fresh one,
so one sum would hide what a Run cost. A call that went through the provider's batch path counts
all its tokens as batch tokens.

`cost_by_protocol` is the report behind `cograil runs --cost`. Its headline is the cost per
resolved run (ADR 0013): the total cost of the Runs that completed, which means without
escalation, divided by their count.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from cograil.domain import Harness, Run, RunStatus
from cograil.harness import call_cost
from cograil.providers.base import Usage

USAGE_KEY = "usage"  # Run.context["usage"]: the Run's token tally


class RunUsage(BaseModel):
    """Tokens by category. `fresh_*` is what was neither cached nor sent through the batch path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    batch_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.batch_tokens
        )

    def __add__(self, other: RunUsage) -> RunUsage:
        return RunUsage(
            **{name: getattr(self, name) + getattr(other, name) for name in RunUsage.model_fields}
        )


def run_usage(run: Run) -> RunUsage:
    """The Run's token tally; all zeros for a Run that has made no model call."""
    return RunUsage.model_validate(run.context.get(USAGE_KEY, {}))


def charge(harness: Harness, run: Run, model: str, usage: Usage) -> Run:
    """`run` with one call's dollars and tokens added. Raises LoopBudgetExceeded as
    `call_cost` does, for an unpriced model under a dollar budget."""
    cost = call_cost(
        harness, model, usage.input_tokens, usage.output_tokens,
        usage.cache_read_tokens, usage.cache_write_tokens,
    )  # fmt: skip
    tally = run_usage(run) + _of_call(usage)
    context: dict[str, Any] = {**run.context, USAGE_KEY: tally.model_dump()}
    return run.model_copy(update={"cost_usd": run.cost_usd + cost, "context": context})


def _of_call(usage: Usage) -> RunUsage:
    if usage.batch:
        total = usage.input_tokens + usage.output_tokens
        return RunUsage(batch_tokens=total + usage.cache_read_tokens + usage.cache_write_tokens)
    return RunUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


class ProtocolCost(BaseModel):
    """What one Protocol's Runs cost. `cost_per_resolved_run` is None when none resolved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: str
    runs: int
    resolved: int
    cost_usd: float
    cost_per_resolved_run: float | None
    usage: RunUsage


def cost_by_protocol(runs: Iterable[Run]) -> list[ProtocolCost]:
    """One row per Protocol, in the order the Protocols first appear."""
    grouped: dict[str, list[Run]] = {}
    for run in runs:
        grouped.setdefault(run.protocol, []).append(run)
    return [_row(name, found) for name, found in grouped.items()]


def _row(protocol: str, runs: Sequence[Run]) -> ProtocolCost:
    resolved = [r for r in runs if r.status is RunStatus.completed]
    tally = RunUsage()
    for run in runs:
        tally = tally + run_usage(run)
    spent = sum(r.cost_usd for r in resolved)
    return ProtocolCost(
        protocol=protocol,
        runs=len(runs),
        resolved=len(resolved),
        cost_usd=sum(r.cost_usd for r in runs),
        cost_per_resolved_run=spent / len(resolved) if resolved else None,
        usage=tally,
    )


def format_cost_table(rows: Sequence[ProtocolCost]) -> list[str]:
    """A line per Protocol: its Runs, tokens by category, cost, and cost per resolved run."""
    if not rows:
        return ["  (no Runs)"]
    lines = [
        f"{'protocol':<20} {'runs':>5} {'resolved':>8} {'fresh_in':>9} {'fresh_out':>9} "
        f"{'cache_read':>10} {'cache_write':>11} {'batch':>8} {'cost':>10} {'cost/resolved':>14}"
    ]
    for r in rows:
        u = r.usage
        per = "-" if r.cost_per_resolved_run is None else f"${r.cost_per_resolved_run:.4f}"
        lines.append(
            f"{r.protocol:<20} {r.runs:>5} {r.resolved:>8} {u.input_tokens:>9} "
            f"{u.output_tokens:>9} {u.cache_read_tokens:>10} {u.cache_write_tokens:>11} "
            f"{u.batch_tokens:>8} {f'${r.cost_usd:.4f}':>10} {per:>14}"
        )
    return lines
