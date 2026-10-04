"""The Harness: versioning, loop bounds and cost (ADR 0008, 0012).

workspace.py loads harness.yaml into `Workspace.harness`; a workspace without one gets the
defaults. The harness version is the file's semantic `version` plus a hash of its validated
content with defaults filled in, such as `1.0.0+3f2a9c1b7d4e`. An edited value changes it,
and so does a changed code default that an omitted key falls back to; comments and layout do
not, since they cannot change behaviour. The runner stamps it on every Run.

Tiers (ADR 0010, 0013): a Step asks for a tier, never a model. Its tier is its own
`(model: ...)`, else its Protocol's, else its Colleague's default_tier; `tier_model` maps it to
the model of the harness's provider. Its effort is its own `(effort: ...)`, else
defaults.effort. Classification, extraction, redaction and compression are small-tier work
(`task_tier`). A small-tier result below defaults.min_confidence escalates one tier.

A Step's inner loop is checked against the bounds before every provider call: its turns
(the Step's `(turns: N)`, else loop.max_turns), the tokens spent in the Step against
token_budget_per_step, and the Run's cost against usd_budget_per_run. So a Step spends at
most a budget plus one call. A breach raises LoopBudgetExceeded, which the runner escalates.
"""

from __future__ import annotations

import hashlib
import json

from cograil.domain import Colleague, Effort, Harness, Protocol, Step, Tier
from cograil.errors import LoopBudgetExceeded

HASH_LENGTH = 12
SMALL_TASKS = frozenset({"classification", "extraction", "redaction", "compression"})
_NEXT_TIER: dict[Tier, Tier] = {"small": "standard", "standard": "strong"}


def harness_version(harness: Harness) -> str:
    """Semantic version plus content hash, as stamped on Run.harness_version."""
    content = json.dumps(harness.model_dump(mode="json"), sort_keys=True)
    digest = hashlib.sha256(content.encode()).hexdigest()[:HASH_LENGTH]
    return f"{harness.version}+{digest}"


def tier_model(harness: Harness, tier: Tier) -> str:
    """The concrete model of `tier` for the harness's provider."""
    return harness.models.model_for(tier)


def task_tier(harness: Harness, task: str) -> Tier:
    """The tier of a kind of work: small for SMALL_TASKS, else the judgment tier."""
    return (
        harness.defaults.classification_tier
        if task in SMALL_TASKS
        else harness.defaults.judgment_tier
    )


def step_tier(colleague: Colleague, protocol: Protocol, step: Step) -> Tier:
    """The Step's tier: its own, else its Protocol's, else its Colleague's default."""
    return step.model_tier or protocol.model_tier or colleague.default_tier


def step_effort(harness: Harness, step: Step) -> Effort | None:
    """The Step's `(effort: ...)`, else defaults.effort; None leaves it to the provider."""
    return step.effort or harness.defaults.effort


def escalation_tier(harness: Harness, tier: Tier, confidence: float | None) -> Tier | None:
    """The next tier up when a small-tier result is below defaults.min_confidence, else None.

    A result without a confidence cannot be judged, so it stands.
    """
    if tier != "small" or confidence is None or confidence >= harness.defaults.min_confidence:
        return None
    return _NEXT_TIER[tier]


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


# What saved context costs against the model's input price (ADR 0013): a cache write is billed
# above it, a cache read well below it.
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.1


def call_cost(
    harness: Harness,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD cost of one provider call; 0.0 for an unpriced model when no dollar budget applies.

    Cache reads and writes are billed at their factors of the input price; they are not part
    of `input_tokens`.

    Under usd_budget_per_run an unpriced model raises LoopBudgetExceeded: its spend could
    not be bounded.
    """
    price = harness.pricing.get(model)
    if price is not None:
        input_units = (
            input_tokens
            + cache_read_tokens * CACHE_READ_FACTOR
            + cache_write_tokens * CACHE_WRITE_FACTOR
        )
        spent = input_units * price.input_per_mtok + output_tokens * price.output_per_mtok
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
