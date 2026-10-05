"""OpenTelemetry tests for issues #28 and #228: one per acceptance criterion."""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.trace import StatusCode

from cograil.cost import charge
from cograil.domain import Colleague, Harness, Price
from cograil.errors import ModelSpanMissing
from cograil.harness import tier_model
from cograil.observability import OTLP_ENDPOINT_ENV, _exporter, model_span, step_span
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, scripted
from cograil.providers.base import Usage
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

PROTOCOL = parse_protocol('Protocol: leave_request\n1. Step "One": Do it.\n2. Step "Two": Again.\n')
PRICES = Harness(pricing={"fake-model": Price(input_per_mtok=1.0, output_per_mtok=2.0)})


async def run_two_steps(store: InMemoryRunStore) -> None:
    colleague = Colleague(
        name="harper", role="r", escalation_contact="x@example.com", protocols=["leave_request"]
    )
    provider = FakeProvider([
        scripted("one", done=True, input_tokens=100, output_tokens=40, cache_read_tokens=500),
        scripted("two", done=True, input_tokens=10, output_tokens=5),
    ])  # fmt: skip
    runner = Runner(provider, ToolRegistry(store), store, colleague, harness=PRICES)
    await runner.run("r1", PROTOCOL)


async def test_model_spans_carry_genai_attributes_under_their_step_and_run(
    store: InMemoryRunStore, spans: Callable[[], list[ReadableSpan]]
) -> None:
    await run_two_steps(store)
    finished = spans()
    by_name = {s.name: s for s in finished}
    run_span = by_name["run leave_request"]
    steps = [s for s in finished if s.name.startswith("step ")]
    chats = [s for s in finished if s.name.startswith("chat ")]
    assert run_span.attributes is not None and run_span.attributes["cograil.run_id"] == "r1"
    assert [s.attributes["cograil.step.number"] for s in steps if s.attributes] == [1, 2]
    assert all(s.parent and s.parent.span_id == run_span.context.span_id for s in steps)
    assert len(chats) == 2
    for chat, step in zip(chats, steps, strict=True):
        assert chat.parent is not None and chat.parent.span_id == step.context.span_id
    first = chats[0].attributes
    assert first is not None
    assert first["gen_ai.operation.name"] == "chat"
    assert first["gen_ai.request.model"] == tier_model(PRICES, "standard")
    assert first["gen_ai.response.model"] == "fake-model"
    assert first["gen_ai.usage.input_tokens"] == 100
    assert first["gen_ai.usage.output_tokens"] == 40
    assert first["gen_ai.usage.cache_read.input_tokens"] == 500
    assert first["cograil.cost_usd"] == pytest.approx((100 + 500 * 0.1 + 2 * 40) / 1_000_000)


async def test_spans_never_carry_message_text(
    store: InMemoryRunStore, spans: Callable[[], list[ReadableSpan]]
) -> None:
    await run_two_steps(store)
    keys = {key for span in spans() for key in (span.attributes or {})}
    assert not {key for key in keys if "prompt" in key or "completion" in key or "content" in key}


def test_spans_go_to_the_console_unless_an_otlp_endpoint_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
    assert isinstance(_exporter(), ConsoleSpanExporter)
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, "http://localhost:6006")
    assert isinstance(_exporter(), OTLPSpanExporter)


def test_a_failing_span_keeps_the_exception_type_but_not_its_message(
    spans: Callable[[], list[ReadableSpan]],
) -> None:
    with pytest.raises(ValueError), model_span("fake-model"):
        raise ValueError("alice@example.com asked about her diagnosis")
    (span,) = spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert span.attributes is not None and span.attributes["exception.type"] == "ValueError"
    assert not span.events
    assert "alice@example.com" not in span.to_json()


async def test_a_charge_outside_a_model_span_is_refused(store: InMemoryRunStore) -> None:
    # The mistake a routing or redaction call would make once it is charged like a Step's own
    # call (#226): its usage would be stamped on whatever span is current, here a Step's.
    run = await store.get_run("r1")
    usage = Usage(input_tokens=10, output_tokens=5)
    with pytest.raises(ModelSpanMissing), step_span(run, PROTOCOL.steps[0]):
        charge(PRICES, run, "fake-model", usage)


# The shared test setup installs a tracer provider at import, and OpenTelemetry allows only one
# per process, so `configure_tracing` runs in a child interpreter, which starts with none.
_TRACING_CHILD = """
import io, json, sys
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

mode = sys.argv[1]
if mode != "off":
    sys.stderr = io.StringIO()  # the console exporter binds this when configure_tracing builds it
from cograil.observability import configure_tracing, model_span, record_model_call
from cograil.providers.base import Usage

was_installed = isinstance(trace.get_tracer_provider(), TracerProvider)
provider = trace.get_tracer_provider()
if mode != "off":
    configure_tracing()
    provider = trace.get_tracer_provider()
    configure_tracing()  # a second call after one is installed changes nothing
with model_span("fake-model"):
    record_model_call("fake-model", Usage(input_tokens=10, output_tokens=5), 0.0)
print(json.dumps({
    "was_installed": was_installed,
    "installed": isinstance(trace.get_tracer_provider(), TracerProvider),
    "idempotent": provider is trace.get_tracer_provider(),
    "flushed": provider.force_flush(10_000) if mode != "off" else None,
    "stderr": sys.stderr.getvalue() if mode != "off" else "",
}))
"""


def _tracing_child(mode: str, endpoint: str | None = None) -> dict[str, Any]:
    """Run `_TRACING_CHILD` in a fresh interpreter, which starts with no tracer provider and
    no inherited OTLP endpoint."""
    env = {k: v for k, v in os.environ.items() if k != OTLP_ENDPOINT_ENV}
    if endpoint is not None:
        env[OTLP_ENDPOINT_ENV] = endpoint
    done = subprocess.run(
        [sys.executable, "-c", _TRACING_CHILD, mode], capture_output=True, text=True,
        env=env, timeout=40, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return dict(json.loads(done.stdout))


def test_configure_tracing_installs_the_provider_and_picks_the_exporter() -> None:
    received: list[str] = []

    class Sink(BaseHTTPRequestHandler):
        """Answers 200 to the OTLP POSTs, so the exporter does not retry."""

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass  # keep the test's own output clean

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    try:
        console = _tracing_child("console")
        otlp = _tracing_child("otlp", f"http://127.0.0.1:{sink.server_address[1]}")
        off = _tracing_child("off")
    finally:
        sink.shutdown()
        sink.server_close()

    assert console["was_installed"] is False and console["installed"] is True
    assert console["idempotent"] is True and console["flushed"] is True
    assert '"name": "chat fake-model"' in console["stderr"]  # one JSON line on the console
    assert otlp["installed"] is True and otlp["flushed"] is True
    assert '"chat fake-model"' not in otlp["stderr"]  # the console is not used beside OTLP
    assert received == ["/v1/traces"]  # the span reached the endpoint, over OTLP
    assert off["was_installed"] is False and off["installed"] is False  # no-ops without it
