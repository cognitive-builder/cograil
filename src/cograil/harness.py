"""The Harness: versioning, loop bounds and cost (ADR 0008, 0012).

workspace.py loads harness.yaml into `Workspace.harness`; a workspace without one gets the
defaults. The harness version is the file's semantic `version` plus a hash of its validated
content with defaults filled in, such as `1.0.0+3f2a9c1b7d4e`. An edited value changes it,
and so does a changed code default that an omitted key falls back to; comments and layout do
not, since they cannot change behaviour. The runner stamps it on every Run.

A Step's inner loop is checked against the bounds before every provider call: its turns
(the Step's `(turns: N)`, else loop.max_turns), the tokens spent in the Step against
token_budget_per_step, and the Run's cost against usd_budget_per_run. So a Step spends at
most a budget plus one call. A breach raises LoopBudgetExceeded, which the runner escalates.
"""

from __future__ import annotations

import hashlib
import json

from cograil.domain import Harness, Step
from cograil.errors import LoopBudgetExceeded

HASH_LENGTH = 12


def harness_version(harness: Harness) -> str:
    """Semantic version plus content hash, as stamped on Run.harness_version."""
    content = json.dumps(harness.model_dump(mode="json"), sort_keys=True)
    digest = hashlib.sha256(content.encode()).hexdigest()[:HASH_LENGTH]
    return f"{harness.version}+{digest}"


def max_turns(harness: Harness, step: Step) -> int:
    """The Step's `(turns: N)` wins over the harness default."""
    return step.max_turns or harness.loop.max_turns


def check_bounds(harness: Harness, step: Step, *, turns: int, tokens: int, cost_usd: float) -> None:
    """Raise LoopBudgetExceeded unless the Step may ask the model once more."""
    loop = harness.loop
    bounds: list[tuple[str, float | None, float]] = [
        ("max_turns", max_turns(harness, step), turns),
        ("token_budget_per_step", loop.token_budget_per_step, tokens),
        ("usd_budget_per_run", loop.usd_budget_per_run, cost_usd),
    ]
    for bound, limit, used in bounds:
        if limit is not None and used >= limit:
            raise LoopBudgetExceeded(
                f"step {step.number}: {bound} of {limit} reached ({used}) without step_complete",
                bound=bound,
                limit=limit,
                used=used,
            )


def call_cost(harness: Harness, model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of one provider call; 0.0 for an unpriced model when no dollar budget applies.

    Under usd_budget_per_run an unpriced model raises LoopBudgetExceeded: its spend could
    not be bounded.
    """
    price = harness.pricing.get(model)
    if price is not None:
        spent = input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
        return spent / 1_000_000
    budget = harness.loop.usd_budget_per_run
    if budget is not None:
        raise LoopBudgetExceeded(
            f"model {model!r} has no price in harness.yaml, so usd_budget_per_run cannot bound it",
            bound="usd_budget_per_run",
            limit=budget,
            used=None,
        )
    return 0.0
