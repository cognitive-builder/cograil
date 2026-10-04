"""OpenTelemetry tests for issue #28: one per acceptance criterion."""

from collections.abc import Callable

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from cograil.domain import Colleague, Harness, Price
from cograil.harness import tier_model
from cograil.observability import OTLP_ENDPOINT_ENV, _exporter
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, scripted
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
    assert [s.attributes["cograil.step"] for s in steps if s.attributes] == [1, 2]
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
