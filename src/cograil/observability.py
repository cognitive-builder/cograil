"""Structured logging and OpenTelemetry spans.

Every log event is one JSON line on the `cograil` logger. The current Run id lives in a context
variable and is stamped on every event.

A Run is a span, each Step inside it a child span, and each model call inside the Step a `chat`
span carrying the GenAI semantic convention attributes (`gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.usage.*`) and the call's dollars as `cograil.cost_usd`.
Spans are an operator's view alongside the audit trail, never a replacement for it: they
carry ids, names, models, tokens and cost, and never prompts, tool arguments or outputs.

`configure_tracing` sends spans to the console (stderr) unless OTEL_EXPORTER_OTLP_ENDPOINT is
set, then to that OTLP endpoint (for example Arize Phoenix). Without it the spans are no-ops.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from pydantic_core import to_jsonable_python

if TYPE_CHECKING:
    from cograil.domain import Run, Step
    from cograil.providers.base import Usage

OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

_logger = logging.getLogger("cograil")


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event; fields must never carry secrets."""
    record: dict[str, Any] = {"event": event, "run_id": run_id.get(), **fields}
    _logger.log(level, json.dumps(to_jsonable_python(record, fallback=str), sort_keys=True))


def configure_tracing() -> None:
    """Install the span exporter: OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set, else the console.

    Called once by an entry point (the CLI, the API app). A second call, or a call after a
    tracer provider is already installed, changes nothing.
    """
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "cograil"}))
    provider.add_span_processor(BatchSpanProcessor(_exporter()))
    trace.set_tracer_provider(provider)


def _exporter() -> SpanExporter:
    if os.environ.get(OTLP_ENDPOINT_ENV):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()  # reads the endpoint and headers from the OTEL_* variables
    return ConsoleSpanExporter(
        out=sys.stderr, formatter=lambda span: span.to_json(indent=None) + "\n"
    )


def _tracer() -> trace.Tracer:
    return trace.get_tracer("cograil")


@contextmanager
def run_span(run: Run, protocol: str) -> Iterator[trace.Span]:
    """The span of one execution of a Run (a `run` or a `resume`)."""
    attributes = {"cograil.run_id": run.id, "cograil.workspace": run.workspace,
                  "cograil.colleague": run.colleague, "cograil.protocol": protocol}  # fmt: skip
    with _tracer().start_as_current_span(f"run {protocol}", attributes=attributes) as span:
        yield span


@contextmanager
def step_span(run: Run, step: Step) -> Iterator[trace.Span]:
    """The span of one Step; its model calls are its children."""
    attributes = {
        "cograil.run_id": run.id,
        "cograil.step": step.number,
        "cograil.step.name": step.name,
    }
    with _tracer().start_as_current_span(
        f"step {step.number} {step.name}", attributes=attributes
    ) as span:
        yield span


@contextmanager
def model_span(model: str) -> Iterator[trace.Span]:
    """The span of one provider call; `charge` adds its tokens and cost once it is known."""
    attributes = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model}
    with _tracer().start_as_current_span(f"chat {model}", attributes=attributes) as span:
        yield span


def record_model_call(model: str, usage: Usage, cost_usd: float) -> None:
    """Stamp the current span with what the call used and cost. Cache tokens are reported
    apart from `gen_ai.usage.input_tokens`, which counts only the fresh prompt (ADR 0013)."""
    span = trace.get_current_span()
    span.set_attributes({
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "gen_ai.usage.cache_read.input_tokens": usage.cache_read_tokens,
        "gen_ai.usage.cache_creation.input_tokens": usage.cache_write_tokens,
        "cograil.cost_usd": cost_usd,
    })  # fmt: skip
