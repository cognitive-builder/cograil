"""The eval report: a line per case, then per Protocol the headline number of ADR 0013, cost
per resolved run (the total cost of the Runs that completed without escalation, divided by
their count)."""

from __future__ import annotations

from collections.abc import Sequence

from cograil.evals.cases import EvalModel
from cograil.evals.suite import CaseResult, Level


class ProtocolSummary(EvalModel):
    protocol: str
    cases: int
    passed: int
    resolved: int
    tokens: int
    cost_usd: float
    cost_per_resolved_run: float | None  # None when no Run of the Protocol resolved


def tokens(result: CaseResult) -> int:
    used = result.usage
    return used.input_tokens + used.output_tokens + used.cache_read_tokens + used.cache_write_tokens


def summarise(results: Sequence[CaseResult]) -> list[ProtocolSummary]:
    """One summary per Protocol, in the order the Protocols first appear."""
    names = list(dict.fromkeys(r.protocol for r in results))
    return [_summary(name, [r for r in results if r.protocol == name]) for name in names]


def _summary(protocol: str, results: list[CaseResult]) -> ProtocolSummary:
    resolved = [r for r in results if r.resolved]
    spent = sum(r.cost_usd for r in resolved)
    return ProtocolSummary(
        protocol=protocol,
        cases=len(results),
        passed=sum(r.passed for r in results),
        resolved=len(resolved),
        tokens=sum(tokens(r) for r in results),
        cost_usd=sum(r.cost_usd for r in results),
        cost_per_resolved_run=spent / len(resolved) if resolved else None,
    )


def format_report(workspace: str, level: Level, results: Sequence[CaseResult]) -> list[str]:
    """The report's lines: each case with its tokens and cost and what did not match, then the
    Protocols' cost per resolved run, then the tally."""
    lines = [f"eval {workspace}: level {level}, {len(results)} cases"]
    for r in results:
        used = r.usage
        lines.append(
            f"{'PASS' if r.passed else 'FAIL'}  {r.id:<40} {r.status:<18} "
            f"in {used.input_tokens} out {used.output_tokens} cache {used.cache_read_tokens}/"
            f"{used.cache_write_tokens}  ${r.cost_usd:.4f}"
        )
        lines += [f"      - {problem}" for problem in r.mismatches]
    lines.append(f"{'protocol':<20} cases passed resolved   tokens       cost  cost/resolved")
    for s in summarise(results):
        per = "-" if s.cost_per_resolved_run is None else f"${s.cost_per_resolved_run:.4f}"
        lines.append(
            f"{s.protocol:<20} {s.cases:>5} {s.passed:>6} {s.resolved:>8} {s.tokens:>8} "
            f"{f'${s.cost_usd:.4f}':>10}  {per:>13}"
        )
    passed = sum(r.passed for r in results)
    lines.append(f"passed {passed}/{len(results)}")
    return lines
