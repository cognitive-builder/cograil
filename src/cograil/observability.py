"""Structured logging and OpenTelemetry spans.

Every log event is one JSON line on the `cograil` logger. The current Run id lives in a context
variable and is stamped on every event.

A Run is a span, each Step inside it a child span, and each model call inside the Step a `chat`
span carrying the GenAI semantic convention attributes (`gen_ai.request.model`,
`gen_ai.response.model`, `gen_ai.usage.*`) and the call's dollars as `cograil.cost_usd`.
Spans are an operator's view alongside the audit trail, never a replacement for it: they
carry ids, names, models, tokens and cost, and never prompts, tool arguments or outputs. An
exception that leaves a span is recorded by its type alone: its message may hold a tool's error
text, which the audit trail redacts.

A call's usage and cost land on its own chat span alone: `record_model_call` refuses to stamp
any other span, so a charge made outside a chat span can never put its tokens on the Step or Run
span (issue #228).

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
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.trace import StatusCode
from pydantic_core import to_jsonable_python

from cograil.errors import ModelSpanMissing

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
def _span(name: str, attributes: dict[str, Any]) -> Iterator[trace.Span]:
    """The current span, which keeps only the type of an exception that leaves it, never its
    message or stack trace: a tool's error text may carry personal data the audit trail redacts."""
    with _tracer().start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            span.set_attribute("exception.type", type(exc).__name__)
            raise


@contextmanager
def run_span(run: Run, protocol: str) -> Iterator[trace.Span]:
    """The span of one execution of a Run (a `run` or a `resume`)."""
    attributes = {"cograil.run_id": run.id, "cograil.workspace": run.workspace,
                  "cograil.colleague": run.colleague, "cograil.protocol": protocol}  # fmt: skip
    with _span(f"run {protocol}", attributes) as span:
        yield span


@contextmanager
def step_span(run: Run, step: Step) -> Iterator[trace.Span]:
    """The span of one Step; its model calls are its children."""
    attributes = {
        "cograil.run_id": run.id,
        "cograil.step.number": step.number,
        "cograil.step.name": step.name,
    }
    with _span(f"step {step.number} {step.name}", attributes) as span:
        yield span


@contextmanager
def model_span(model: str) -> Iterator[trace.Span]:
    """The span of one provider call; `charge` adds its tokens and cost once it is known."""
    attributes = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model}
    with _span(f"chat {model}", attributes) as span:
        yield span


def record_model_call(model: str, usage: Usage, cost_usd: float) -> None:
    """Stamp the chat span of the call with what it used and cost. Cache tokens are reported
    apart from `gen_ai.usage.input_tokens`, which counts only the fresh prompt (ADR 0013).

    A charge made while another span is current — a Step's, the Run's — would put its usage on
    that span, so it is refused with ModelSpanMissing instead. With no span current there is
    nothing to stamp, as when tracing is off, and the charge is only accounted, not traced."""
    span = trace.get_current_span()
    if not isinstance(span, ReadableSpan):
        return  # no live span is current — tracing is off, or no span was started
    attributes = span.attributes or {}
    if attributes.get("gen_ai.operation.name") != "chat":
        raise ModelSpanMissing(
            f"a call of {model} was charged on the span {span.name!r}, not on its chat span"
        )
    span.set_attributes({
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "gen_ai.usage.cache_read.input_tokens": usage.cache_read_tokens,
        "gen_ai.usage.cache_creation.input_tokens": usage.cache_write_tokens,
        "cograil.cost_usd": cost_usd,
    })  # fmt: skip
