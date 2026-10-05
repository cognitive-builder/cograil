"""The eval harness and the example-smb golden set, one test per acceptance criterion of issue
#30: the case format, `cograil eval` on FakeProvider unless COGRAIL_LIVE=1, the 25 committed
cases, the three levels, and tokens and cost per case with cost per resolved run per Protocol.

The fake level runs here, in the unit suite, on every pull request at $0. The live levels are
exercised with a stand-in for the live provider, so no test calls a real model.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cograil.evals.cli
from cograil.cli import app
from cograil.domain import Effort, Harness, Step, Tool
from cograil.evals import CaseResult, EvalCase, Level, load_cases, run_case, summarise
from cograil.evals.suite import Suite, level_harness, run_suite
from cograil.providers import FakeProvider, Message, Plan, Usage, scripted
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "workspaces/example-smb"
GOLDEN = Path(__file__).parent / "example-smb.jsonl"
CASES = load_cases(GOLDEN)
runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COGRAIL_LIVE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_the_golden_set_holds_at_least_25_cases_covering_every_outcome() -> None:
    assert len(CASES) >= 25
    assert {c.protocol for c in CASES} == {"leave_request", "policy_question", "it_access_request"}
    outcomes = {c.expect.status for c in CASES}
    assert outcomes == {"completed", "awaiting_approval", "escalated", "failed", "denied"}
    assert any(c.expect.gates for c in CASES) and any(c.smoke for c in CASES)


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in CASES])
async def test_each_golden_case_passes_on_the_fake_model(case: EvalCase) -> None:
    workspace = load_workspace(EXAMPLE)
    (result,) = await run_suite(
        workspace, EXAMPLE, [case], Level.fake, lambda c: FakeProvider(c.plans())
    )
    assert result.mismatches == []
    assert result.cost_usd == 0.0


def test_a_case_names_prompt_principal_tool_calls_gates_and_status(tmp_path: Path) -> None:
    """A case that expects the wrong key arg of a gate fails, and the report says why."""
    (case,) = [c for c in CASES if c.id == "leave-annual-awaits-approval"]
    wrong = case.model_dump(mode="json")
    wrong["expect"]["gates"][0]["args"]["employee"] = "bob"
    cases = tmp_path / "cases.jsonl"
    cases.write_text(case.model_dump_json() + "\n" + _renamed(wrong, "wrong-gate") + "\n")
    result = runner.invoke(app, ["eval", str(EXAMPLE), "--cases", str(cases)])
    assert result.exit_code == 1, result.output
    assert "PASS  leave-annual-awaits-approval" in result.output
    assert "FAIL  wrong-gate" in result.output
    assert "gate 1 (hris.submit_leave): employee expected 'bob', got 'alice'" in result.output
    assert "passed 1/2" in result.output


def _renamed(case: dict[str, Any], new_id: str) -> str:
    return EvalCase.model_validate({**case, "id": new_id}).model_dump_json()


@pytest.mark.parametrize(
    ("line", "problem"),
    [('{"id": "x", "protocol": "leave_request"}', ":1: invalid case"),
     ('{"id": "x", "protocol": "p", "principal": "a", "prompt": "b", "expect": '
      '{"status": "completed"}, "surprise": 1}', ":1: invalid case")],
)  # fmt: skip
def test_an_invalid_case_file_is_refused_naming_the_line(
    tmp_path: Path, line: str, problem: str
) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(line + "\n")
    result = runner.invoke(app, ["eval", str(EXAMPLE), "--cases", str(cases)])
    assert result.exit_code == 1 and problem in result.output


def test_eval_runs_every_case_on_the_fake_model_without_cograil_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)  # the default golden set is tests/evals/<workspace name>.jsonl
    result = runner.invoke(app, ["eval", "workspaces/example-smb"])
    assert result.exit_code == 0, result.output
    assert "level fake, 31 cases" in result.output and "passed 31/31" in result.output


class LiveStandIn:
    """Stands in for the live provider: completes every Step at once, naming the model asked."""

    def __init__(self) -> None:
        self.models: list[str | None] = []

    async def plan(
        self,
        step: Step,
        context: Sequence[Message],
        tools: Sequence[Tool],
        *,
        model: str | None = None,
        effort: Effort | None = None,
        prefix: str = "",
    ) -> Plan:
        self.models.append(model)
        return scripted("done", done=True).model_copy(update={"model": model})


def test_cograil_live_runs_the_smoke_subset_on_the_small_tier_through_the_live_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = LiveStandIn()
    made: list[str] = []

    def make_provider(harness: Harness, model: str) -> LiveStandIn:
        made.append(model)
        return live

    monkeypatch.setattr(cograil.evals.cli, "make_provider", make_provider)
    monkeypatch.setenv("COGRAIL_LIVE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    result = runner.invoke(app, ["eval", str(EXAMPLE), "--cases", str(GOLDEN)])
    # The stand-in completes every Step without a tool call, so most cases fail their
    # expectations: this test is about which cases ran and on which model, not the tally.
    smoke = [c.id for c in CASES if c.smoke]
    assert f"level smoke, {len(smoke)} cases" in result.output, result.output
    assert all(case_id in result.output for case_id in smoke)
    assert made == ["claude-haiku-4-5"]
    assert live.models and set(live.models) == {"claude-haiku-4-5"}


@pytest.mark.parametrize(
    ("env", "args", "message"),
    [({}, ["--level", "smoke"], "set COGRAIL_LIVE=1"),
     ({"COGRAIL_LIVE": "1", "ANTHROPIC_API_KEY": "k"}, ["--level", "full"], "batch path"),
     ({}, ["--level", "full"], "batch path"),
     ({"COGRAIL_LIVE": "1"}, [], "ANTHROPIC_API_KEY is not set")],
)  # fmt: skip
def test_a_live_level_runs_only_when_allowed_and_full_only_through_the_batch_path(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], args: list[str], message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    result = runner.invoke(app, ["eval", str(EXAMPLE), "--cases", str(GOLDEN), *args])
    assert result.exit_code == 1 and message in result.output


def test_the_levels_choose_their_cases_and_tiers() -> None:
    harness = load_workspace(EXAMPLE).harness
    smoke = level_harness(harness, Level.smoke).models
    assert {smoke.small, smoke.standard, smoke.strong} == {"claude-haiku-4-5"}
    assert level_harness(harness, Level.full) == harness
    assert level_harness(harness, Level.fake).pricing["fake-model"].input_per_mtok == 0


async def test_each_case_records_its_tokens_and_the_runs_cost() -> None:
    workspace = load_workspace(EXAMPLE)
    (case,) = [c for c in CASES if c.id == "leave-annual-approved"]
    live = LiveStandIn()
    suite = Suite(workspace, EXAMPLE, level_harness(workspace.harness, Level.smoke),
                  lambda c: live, live=False)  # fmt: skip
    result = await run_case(suite, case)
    calls = len(live.models)  # one per Step: each completes at once
    assert result.usage == Usage(input_tokens=10 * calls, output_tokens=5 * calls)
    assert result.cost_usd == pytest.approx(calls * (10 * 1.00 + 5 * 5.00) / 1_000_000)


def test_the_report_gives_cost_per_resolved_run_per_protocol() -> None:
    def result(protocol: str, status: str, cost: float) -> CaseResult:
        return CaseResult(id=f"{protocol}-{status}-{cost}", protocol=protocol, status=status,
                          mismatches=[], usage=Usage(input_tokens=100), cost_usd=cost)  # fmt: skip

    leave, policy = summarise([
        result("leave", "completed", 0.02), result("leave", "completed", 0.04),
        result("leave", "escalated", 0.10), result("policy", "awaiting_approval", 0.01),
    ])  # fmt: skip
    assert (leave.cases, leave.resolved, leave.tokens) == (3, 2, 300)
    assert leave.cost_per_resolved_run == pytest.approx(0.03)
    assert leave.cost_usd == pytest.approx(0.16)
    assert policy.resolved == 0 and policy.cost_per_resolved_run is None
