"""Tier tests for issue #48: tiers instead of model names, effort, escalation, Ollama."""

from types import SimpleNamespace
from typing import Any

import pytest

from cograil.domain import (
    Colleague,
    Harness,
    Protocol,
    RunStatus,
    Step,
    TierDefaults,
    TierModels,
    Tool,
)
from cograil.errors import ProviderError, ToolNotAllowed
from cograil.harness import (
    escalation_tier,
    step_effort,
    step_tier,
    task_tier,
    tier_model,
)
from cograil.parser import parse_protocol
from cograil.providers import FakeProvider, OllamaProvider, PlannedToolCall, scripted
from cograil.registry import ToolRegistry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore

OLLAMA = {"ollama": TierModels(small="gemma3:4b", standard="qwen3:14b", strong="qwen3:32b")}
STEP = Step(number=1, name="Look up", instruction="Find the balance.")
HARPER = Colleague(name="harper", role="HR", escalation_contact="x@example.com", protocols=["p"])


def test_default_models_per_tier_and_per_provider() -> None:
    harness = Harness()
    assert [tier_model(harness, t) for t in ("small", "standard", "strong")] == [
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
        "claude-opus-5-5",
    ]
    local = Harness(provider="ollama", providers=OLLAMA)
    assert tier_model(local, "small") == "gemma3:4b"
    with pytest.raises(ValueError, match="needs its models"):
        Harness(provider="ollama")


@pytest.mark.parametrize(
    ("task", "tier"),
    [("classification", "small"), ("extraction", "small"), ("redaction", "small"),
     ("compression", "small"), ("judgment", "standard")],
)  # fmt: skip
def test_small_work_defaults_to_the_small_tier(task: str, tier: str) -> None:
    assert task_tier(Harness(), task) == tier


@pytest.mark.parametrize(
    ("step_tier_", "protocol_tier", "expected"),
    [("small", "strong", "small"), (None, "strong", "strong"), (None, None, "standard")],
)
def test_a_step_resolves_its_tier_from_step_then_protocol_then_colleague(
    step_tier_: Any, protocol_tier: Any, expected: str
) -> None:
    step = STEP.model_copy(update={"model_tier": step_tier_})
    protocol = Protocol(name="p", steps=[step], model_tier=protocol_tier)
    assert step_tier(HARPER, protocol, step) == expected
    assert step_tier(HARPER.model_copy(update={"default_tier": "small"}), protocol, step) == (
        expected if step_tier_ or protocol_tier else "small"
    )


@pytest.mark.parametrize(
    ("directive", "default", "expected"),
    [("(effort: high)", None, "high"), ("", "low", "low"), ("(effort: low)", "high", "low"),
     ("", None, None)],
)  # fmt: skip
def test_effort_is_the_step_directive_else_the_harness_default(
    directive: str, default: Any, expected: Any
) -> None:
    protocol = parse_protocol(f'Protocol: p\n1. Step "A": Do it. {directive}')
    harness = Harness(defaults=TierDefaults(effort=default))
    assert step_effort(harness, protocol.steps[0]) == expected


@pytest.mark.parametrize(
    ("tier", "confidence", "expected"),
    [("small", 0.4, "standard"), ("small", 0.7, None), ("small", None, None),
     ("standard", 0.1, None)],
)  # fmt: skip
def test_only_a_low_confidence_small_result_escalates_one_tier(
    tier: Any, confidence: Any, expected: Any
) -> None:
    assert escalation_tier(Harness(), tier, confidence) == expected


async def test_a_low_confidence_small_result_is_asked_again_one_tier_up_and_audited(
    store: InMemoryRunStore,
) -> None:
    protocol = parse_protocol('Protocol: p\n1. Step "A": Answer. (model: small; effort: high)')
    provider = FakeProvider(
        [scripted("unsure", done=True, confidence=0.2), scripted("sure", done=True, confidence=0.9)]
    )
    registry = ToolRegistry(store)
    run = await Runner(provider, registry, store, HARPER).run("r1", protocol)
    assert [(c.model, c.effort) for c in provider.calls] == [
        ("claude-haiku-4-5", "high"),
        ("claude-sonnet-5-5", "high"),
    ]
    assert run.context["steps"]["1"]["output"] == "sure"
    escalated = [e for e in await store.list_audit_events("r1") if e.kind == "tier.escalated"]
    assert len(escalated) == 1
    assert escalated[0].principal_id == "alice@example.com"
    assert escalated[0].detail == {"step": 1, "from_tier": "small", "to_tier": "standard",
                                   "confidence": 0.2, "reason": "low confidence"}  # fmt: skip


async def test_a_confident_or_standard_tier_result_does_not_escalate(
    store: InMemoryRunStore,
) -> None:
    protocol = parse_protocol('Protocol: p\n1. Step "A": Answer. (model: small)\n'
                              '2. Step "B": Answer again.')  # fmt: skip
    provider = FakeProvider([scripted("ok", done=True, confidence=0.9),
                             scripted("hm", done=True, confidence=0.1)])  # fmt: skip
    await Runner(provider, ToolRegistry(store), store, HARPER).run("r1", protocol)
    assert [c.model for c in provider.calls] == ["claude-haiku-4-5", "claude-sonnet-5-5"]
    assert not [e for e in await store.list_audit_events("r1") if e.kind == "tier.escalated"]


WRITE = Tool(name="hris.submit", kind="python", scope="write", confirm_before_write=True)
SUBMIT = PlannedToolCall(id="c1", tool="hris.submit", args={"days": 3})
OTHER = PlannedToolCall(id="c2", tool="hris.other", args={})
GATED = parse_protocol('Protocol: p\n1. Step "A": Use @hris.submit to book it. (model: small)')


@pytest.fixture
def invoked() -> list[str]:
    return []


def write_registry(store: InMemoryRunStore, invoked: list[str]) -> ToolRegistry:
    registry = ToolRegistry(store)

    async def invoke(args: dict[str, Any]) -> dict[str, Any]:
        invoked.append("hris.submit")
        return {"ok": True}

    registry.register(WRITE, invoke)
    return registry


async def test_a_discarded_small_plan_runs_nothing_and_the_escalated_plan_stays_gated(
    store: InMemoryRunStore, invoked: list[str]
) -> None:
    """The low-confidence plan's write is dropped; the standard plan's write needs approval."""
    low = scripted("", SUBMIT, done=True, confidence=0.2)
    provider = FakeProvider([low, scripted("", SUBMIT)])
    runner = Runner(provider, write_registry(store, invoked), store, HARPER)
    run = await runner.run("r1", GATED)
    assert invoked == [] and run.status is RunStatus.awaiting_approval
    assert [c.model for c in provider.calls] == ["claude-haiku-4-5", "claude-sonnet-5-5"]


async def test_the_escalated_plan_is_held_to_the_step_whitelist(
    store: InMemoryRunStore, invoked: list[str]
) -> None:
    provider = FakeProvider([scripted("", done=True, confidence=0.2), scripted("", OTHER)])
    runner = Runner(provider, write_registry(store, invoked), store, HARPER)
    with pytest.raises(ToolNotAllowed):
        await runner.run("r1", GATED)
    assert invoked == []


class StubOllama:
    def __init__(self, response: Any) -> None:
        self.response, self.requests = response, []

    async def chat(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self.response


def ollama_response(*calls: tuple[str, dict[str, Any]]) -> Any:
    uses = [SimpleNamespace(function=SimpleNamespace(name=n, arguments=a)) for n, a in calls]
    message = SimpleNamespace(content="Checking.", tool_calls=uses)
    return SimpleNamespace(message=message, prompt_eval_count=40, eval_count=7, done_reason="stop")


async def test_the_ollama_provider_plans_with_a_local_model() -> None:
    tool = Tool(name="hris.get_balance", kind="python", scope="read", description="Balance")
    client = StubOllama(
        ollama_response(
            ("hris__get_balance", {"employee": "alice"}),
            ("step_complete", {"output": "25", "confidence": 0.9}),
        )
    )
    provider = OllamaProvider("gemma3:4b", client=client)
    plan = await provider.plan(STEP, [], [tool], model="qwen3:14b", effort="high")
    request = client.requests[0]
    assert request["model"] == "qwen3:14b" and plan.model == "qwen3:14b"
    assert request["messages"][0]["role"] == "system"
    assert [t["function"]["name"] for t in request["tools"]] == [
        "hris__get_balance",
        "step_complete",
    ]
    assert [(c.tool, c.args) for c in plan.tool_calls] == [
        ("hris.get_balance", {"employee": "alice"})
    ]
    assert plan.step_complete is not None and plan.step_complete.confidence == 0.9
    assert (plan.usage.input_tokens, plan.usage.output_tokens) == (40, 7)


async def test_an_ollama_failure_is_a_provider_error() -> None:
    class Down:
        async def chat(self, **_: Any) -> Any:
            raise ConnectionError("refused")

    with pytest.raises(ProviderError, match="Ollama call failed"):
        await OllamaProvider("gemma3:4b", client=Down()).plan(STEP, [], [])
