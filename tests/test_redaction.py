"""Redaction of tool error text before it is persisted (issue #78).

The owner's decision: patterns first, then the small tier through the Provider interface;
redact before saving; the model still sees the raw error during the Run. Every string here is
fake. No real model is called."""

import json
from typing import Any

import pytest

from cograil.domain import Colleague, Tool, Workspace
from cograil.errors import ToolExecutionError
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.redaction import REDACT_TOOL, Redactor, redact_patterns, redaction_model
from cograil.registry import CallContext, ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

PII = "bob.builder@example.com"
KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE1234"
RAW = f"{PII} not found; key {KEY}"


def answer(text: str) -> FakeProvider:
    return FakeProvider(
        [scripted("", PlannedToolCall(id="r1", tool=REDACT_TOOL, args={"text": text}))]
    )


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (f"no employee {PII}", PII),
        (f"auth failed with {KEY}", KEY),
        ("rejected sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2", "a1B2c3D4e5F6g7H8i9J0k1L2"),
        ("401 for ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3", "A1b2C3d4E5f6G7h8I9j0K1l2M3"),
        ("Authorization: Bearer abcDEF123456.token-value", "abcDEF123456.token-value"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl", "eyJhbGciOiJIUzI1NiJ9"),
        ("connect postgresql://app:hunter2@db.internal:5432/hris", "hunter2"),
        ("GET https://carol:s3cr3t@hris.example.com/v1/leave", "s3cr3t"),
        ("Server=db.internal;Database=hris;User Id=app;Password=hunter2", "hunter2"),
        ("Server=db.internal;Database=hris;User Id=app;Password=hunter2", "db.internal"),
        ("bad request /x?api_key=abc123&days=3", "abc123"),
        ('login failed {"password": "hunter2"}', "hunter2"),
    ],
)
def test_patterns_mask_what_must_not_be_stored(raw: str, secret: str) -> None:
    masked = redact_patterns(raw)
    assert secret not in masked
    assert "[REDACTED:" in masked
    assert redact_patterns(masked) == masked  # redacting twice changes nothing


def test_patterns_keep_the_rest_of_the_error() -> None:
    raw = "ToolExecutionError: hris.get_balance: HTTP 503 from https://hris.example.com/v1/leave"
    assert redact_patterns(raw) == raw


async def test_redactor_without_a_provider_applies_the_patterns_only() -> None:
    assert await Redactor().redact(RAW) == redact_patterns(RAW)
    assert await Redactor().redact("") == ""


async def test_small_tier_pass_catches_what_patterns_miss() -> None:
    raw = "no leave record for Robert Tables in Berlin"
    provider = answer("no leave record for [REDACTED:name] in [REDACTED:address]")
    redacted = await Redactor(provider).redact(raw)
    assert redacted == "no leave record for [REDACTED:name] in [REDACTED:address]"
    [offered] = provider.calls
    assert [t.name for t in offered.tools] == [REDACT_TOOL]
    assert offered.context[0].content == raw


async def test_the_model_only_sees_text_the_patterns_already_masked() -> None:
    provider = answer("whatever")
    await Redactor(provider).redact(RAW)
    seen = provider.calls[0].context[0].content
    assert PII not in seen and KEY not in seen


async def test_a_model_answer_that_leaks_is_masked_again() -> None:
    assert PII not in await Redactor(answer(f"contact {PII}")).redact("some failure")


@pytest.mark.parametrize(
    "provider",
    [FakeProvider([]), FakeProvider([scripted("no tool call")])],
    ids=["provider-fails", "no-answer"],
)
async def test_when_the_small_tier_fails_the_patterns_result_stands(provider: FakeProvider) -> None:
    assert await Redactor(provider).redact(RAW) == redact_patterns(RAW)


def test_redaction_runs_on_the_small_tier() -> None:
    ws = Workspace(name="w", colleagues=[], protocols=[], tools=[])
    assert redaction_model(ws) == ws.harness.tiers.small == "claude-haiku-4-5"


# The persisted copy is redacted; the caller and the model keep the raw text.

CTX = CallContext(run_id="r1", step=1, principal_id="alice@example.com")
TOOL = Tool(name="hris.get_balance", kind="python", scope="read")


async def _raise(args: dict[str, Any]) -> None:
    raise RuntimeError(RAW)


async def test_the_tool_call_and_the_audit_event_hold_the_redacted_error(
    store: InMemoryRunStore,
) -> None:
    registry = ToolRegistry(store, Redactor(answer("never used")))
    registry.register(TOOL, _raise)
    with pytest.raises(ToolExecutionError) as raised:
        await registry.invoke("hris.get_balance", {}, CTX)
    assert PII in str(raised.value)  # the caller, and so the model, gets the raw error
    [call] = await store.list_tool_calls("r1")
    [event] = await store.list_audit_events("r1")
    for stored in (call.error, event.detail["error"]):
        assert stored is not None and PII not in stored and KEY not in stored


async def test_a_registry_without_a_redactor_still_masks_with_patterns(
    store: InMemoryRunStore,
) -> None:
    registry = ToolRegistry(store)
    registry.register(TOOL, _raise)
    with pytest.raises(ToolExecutionError):
        await registry.invoke("hris.get_balance", {}, CTX)
    [call] = await store.list_tool_calls("r1")
    assert call.error is not None and PII not in call.error and "[REDACTED:email]" in call.error


PROTOCOL = """
Protocol: demo
1. Step "Look up": Use @hris.get_balance for the requester.

Error handling:
- @hris.get_balance fails: retry once, then escalate to the Human Manager.
"""
HARPER = Colleague(
    name="harper", role="HR", escalation_contact="hr@example.com", protocols=["demo"]
)
LOOK_UP = scripted("", PlannedToolCall(id="c", tool="hris.get_balance", args={}))


async def test_the_model_sees_the_raw_error_and_the_escalation_audit_is_redacted(
    store: InMemoryRunStore,
) -> None:
    registry = ToolRegistry(store)
    registry.register(TOOL, _raise)
    provider = FakeProvider([LOOK_UP, LOOK_UP])
    run = await Runner(provider, registry, store, HARPER).run("r1", parse_protocol(PROTOCOL))
    assert run.status == "escalated"
    assert PII in provider.calls[1].context[-1].content  # the raw error went back to the model
    stored = json.dumps(
        [e.detail for e in await store.list_audit_events("r1")]
        + [c.model_dump(mode="json") for c in await store.list_tool_calls("r1")]
    )
    assert PII not in stored and KEY not in stored


async def test_a_run_that_fails_closed_audits_the_redacted_message(store: InMemoryRunStore) -> None:
    registry = ToolRegistry(store)
    registry.register(TOOL, _raise)
    protocol = parse_protocol(PROTOCOL.split("Error handling")[0])
    with pytest.raises(ToolExecutionError):
        await Runner(FakeProvider([LOOK_UP]), registry, store, HARPER).run("r1", protocol)
    [failed] = [e for e in await store.list_audit_events("r1") if e.kind == "run.failed"]
    assert PII not in failed.detail["message"]
    assert "[REDACTED:email]" in failed.detail["message"]
