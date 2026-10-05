"""The example protocol library: one test per acceptance criterion of issue #35.

Both example workspaces carry leave_request, it_access_request and policy_question (with
citations), and each Protocol has eval cases that pass on the FakeProvider.
"""

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from cograil.domain import Protocol, Workspace
from cograil.errors import ToolExecutionError
from cograil.evals import EvalCase, Level, load_cases
from cograil.evals.suite import run_suite
from cograil.providers import FakeProvider
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[1]
LIBRARY = ("leave_request", "it_access_request", "policy_question")
WORKSPACES = ("example-smb", "example-enterprise")
CITATION = re.compile(r'"[^"]+" \([^()]+, [^()]+\)')  # "verbatim quote" (Title, 1. Section)


def _workspace(name: str) -> Workspace:
    return load_workspace(ROOT / "workspaces" / name)


def _cases(name: str) -> list[EvalCase]:
    return load_cases(ROOT / "tests/evals" / f"{name}.jsonl")


@pytest.mark.parametrize("name", WORKSPACES)
def test_each_workspace_has_the_three_protocols_run_by_a_colleague(name: str) -> None:
    workspace = _workspace(name)
    protocols = {p.name for p in workspace.protocols}
    assert set(LIBRARY) <= protocols
    runnable = {p for c in workspace.colleagues for p in c.protocols}
    assert set(LIBRARY) <= runnable


@pytest.mark.parametrize("name", WORKSPACES)
def test_policy_question_searches_knowledge_and_asks_for_citations(name: str) -> None:
    workspace = _workspace(name)
    protocol = next(p for p in workspace.protocols if p.name == "policy_question")
    assert any("knowledge.search" in step.tools for step in protocol.steps)
    answer = next(s for s in protocol.steps if s.name == "Answer")
    assert "cite the document title and section" in answer.instruction
    assert any("verbatim quote" in rule for rule in protocol.guardrails)
    answered = [
        plan.text
        for case in _cases(name)
        if case.protocol == "policy_question"
        for plan in case.script
        if plan.text.startswith('"')
    ]
    assert answered and all(CITATION.fullmatch(text) for text in answered)


@pytest.mark.parametrize("name", WORKSPACES)
@pytest.mark.parametrize("protocol", LIBRARY)
def test_each_protocol_has_eval_cases_for_a_happy_path_and_a_refusal(
    name: str, protocol: str
) -> None:
    cases = [c for c in _cases(name) if c.protocol == protocol]
    statuses = {c.expect.status for c in cases}
    assert len(cases) >= 3
    assert "denied" in statuses  # the audience check refuses an unknown principal
    assert statuses - {"denied"}  # and at least one case runs the Protocol


@pytest.mark.parametrize("name", WORKSPACES)
async def test_every_library_case_passes_on_the_fake_model(name: str) -> None:
    workspace, root = _workspace(name), ROOT / "workspaces" / name
    cases = [c for c in _cases(name) if c.protocol in LIBRARY]
    results = await run_suite(workspace, root, cases, Level.fake, lambda c: FakeProvider(c.plans()))
    assert len(results) == len(cases)
    assert [(r.id, r.mismatches) for r in results if not r.passed] == []


@pytest.mark.parametrize("name", WORKSPACES)
def test_a_write_in_the_library_is_gated(name: str) -> None:
    workspace = _workspace(name)
    tools = {t.name: t for t in workspace.tools}
    library: list[Protocol] = [p for p in workspace.protocols if p.name in LIBRARY]
    written = {
        tool
        for protocol in library
        for step in protocol.steps
        for tool in step.tools
        if tools[tool].scope == "write" and tool != "notify.send"
    }
    assert written and all(tools[t].confirm_before_write for t in written)


def test_the_example_access_register_is_idempotent_and_refuses_what_is_held() -> None:
    it = _load_it()
    first = it.request_access("alice", "crm", "read", "req-1")
    assert it.request_access("alice", "crm", "read", "req-1") is first
    with pytest.raises(ToolExecutionError, match="already holds"):
        it.request_access("alice", "vpn", "user", "req-2")
    with pytest.raises(ToolExecutionError, match="unknown system"):
        it.request_access("alice", "mainframe", "read", "req-3")


def _load_it() -> Any:
    spec = importlib.util.spec_from_file_location("it", ROOT / "workspaces/example-smb/tools/it.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
