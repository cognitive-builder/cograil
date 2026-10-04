"""Compression of long Tool results by the small tier (ADR 0007, 0010).

A Tool result above `Harness.context.compression_threshold_tokens` (estimated, as the Window
Ledger counts) is summarised before it reaches the model. The summary is written by the
`compression` task's tier, small by default, and the Step's own instruction is its guide: it
keeps what the Step may still need. The raw output stays in the Step's recorded `tool_calls` on
the Run, for audit and the run history; only the model's copy is the summary. A later Step that
declares this Step's output sees the summary too, so the saving holds.

The raw output goes to the small model inside a data block (rule 7). Each compression is an
AuditEvent (`context.compressed`) with the Run's principal, and its tokens and cost count against
the Step and the Run like any model call. The Window Ledger keeps the raw and the compressed
size of what was compressed, per Step. A compression that fails raises, and the Run fails closed:
the raw output is never passed on in its place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cograil.claims import RunClaims
from cograil.context import (
    RECORD_KEY,
    add_compression,
    compressed_result,
    data_message,
    dump,
    estimate_tokens,
)
from cograil.cost import charge
from cograil.domain import Harness, Run, Step
from cograil.errors import ProviderError
from cograil.gates import StepProgress
from cograil.harness import task_tier, tier_model
from cograil.observability import model_span
from cograil.providers.base import Provider

GUIDE = (
    "Summarise the Tool output in the data block so that the Step below can still do its work. "
    "Keep every fact, identifier, number, name and date the Step may need, and drop the rest. "
    "Reply with the summary only, as plain text.\n\nThe Step you are summarising for: "
)


def shown(done: Sequence[dict[str, Any]], summaries: Sequence[str | None]) -> list[dict[str, Any]]:
    """`done` as the model sees it: each compressed call's result is its summary."""
    return [
        call if summary is None else {**call, "result": compressed_result(summary)}
        for call, summary in zip(done, summaries, strict=True)
    ]


def recorded(
    done: Sequence[dict[str, Any]], summaries: Sequence[str | None]
) -> list[dict[str, Any]]:
    """`done` for the Step's record: the raw result stays, with its summary beside it."""
    return [
        call if summary is None else {**call, RECORD_KEY: summary}
        for call, summary in zip(done, summaries, strict=True)
    ]


class Compressor:
    def __init__(self, provider: Provider, harness: Harness, claims: RunClaims) -> None:
        self._provider = provider
        self._harness = harness
        self._claims = claims

    async def compress(
        self, run: Run, step: Step, progress: StepProgress, done: Sequence[dict[str, Any]]
    ) -> tuple[Run, list[str | None]]:
        """The summary of each call's result that is over the threshold, else None; in order."""
        summaries: list[str | None] = []
        for call in done:
            run, summary = await self._one(run, step, progress, call)
            summaries.append(summary)
        return run, summaries

    async def _one(
        self, run: Run, step: Step, progress: StepProgress, call: dict[str, Any]
    ) -> tuple[Run, str | None]:
        if "result" not in call:  # an error is short and is the model's to read
            return run, None
        threshold = self._harness.context.compression_threshold_tokens
        raw = estimate_tokens(dump(call["result"]))
        if raw <= threshold:
            return run, None
        guide = Step(number=step.number, name="Compress a Tool output",
                     instruction=f"{GUIDE}{step.name}\n{step.instruction}")  # fmt: skip
        model = tier_model(self._harness, task_tier(self._harness, "compression"))
        messages = [data_message([(f"tool {call['tool']}", call["result"])])]
        with model_span(model):
            plan = await self._provider.plan(guide, messages, [], model=model)
            summary = (
                plan.text or (plan.step_complete.output if plan.step_complete else "")
            ).strip()
            if not summary:
                raise ProviderError(f"compression of {call['tool']} returned no summary")
            usage = plan.usage
            progress.tokens += usage.input_tokens + usage.output_tokens
            run = charge(self._harness, run, plan.model, usage)
        compressed = estimate_tokens(dump(compressed_result(summary)))
        detail = {"step": step.number, "tool": call["tool"], "raw_tokens": raw,
                  "compressed_tokens": compressed, "threshold": threshold}  # fmt: skip
        await self._claims.audit(run, "context.compressed", detail)
        context = add_compression(run.context, step.number, raw, compressed)
        return run.model_copy(update={"context": context}), summary
