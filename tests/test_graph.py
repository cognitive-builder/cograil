"""Tests for issue #46: `cograil graph` renders the compiled Protocol as Mermaid (ADR 0011)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cograil.cli import app
from cograil.domain import Colleague, Protocol, Step, Tool, Workspace
from cograil.errors import WorkspaceError
from cograil.graph import compile_graph, render_mermaid

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
GOLDEN = ROOT / "docs/graphs"

runner = CliRunner()


@pytest.mark.parametrize("protocol", ["leave_request", "policy_question"])
def test_cli_output_matches_the_golden_file(protocol: str) -> None:
    result = runner.invoke(app, ["graph", str(EXAMPLE), "--protocol", protocol])
    assert result.exit_code == 0, result.output
    assert result.output == (GOLDEN / f"{protocol}.mmd").read_text()


def test_leave_request_shows_steps_gate_and_error_edges() -> None:
    result = runner.invoke(app, ["graph", str(EXAMPLE), "--protocol", "leave_request"])
    lines = result.output.splitlines()
    assert lines[0] == "flowchart TD"
    assert all(f"step_{n}[" in result.output for n in (1, 2, 3, 4))
    assert '  step_3 -->|"write"| gate_3' in lines
    assert '  gate_3 -.->|"declined or expired"| escalated' in lines
    assert '  step_1 -.->|"hris.get_balance fails 2x"| escalated' in lines
    assert "  gate_3 --> step_4" in lines  # approval lets the Run go on in step order


def test_a_protocol_without_a_gate_or_threshold_has_no_escalated_node() -> None:
    graph = compile_graph(*_workspace(helpers=[]))
    assert [n.id for n in graph.nodes] == ["start", "done", "step_1"]
    assert [(e.source, e.target) for e in graph.edges] == [
        ("start", "step_1"),
        ("step_1", "done"),
    ]


def test_a_helper_protocol_is_a_subgraph() -> None:
    workspace, main = _workspace(helpers=["lookup"])
    drawn = render_mermaid(compile_graph(workspace, main))
    assert '  subgraph helper_lookup["helper: lookup"]' in drawn
    assert '    helper_lookup_step_1["1. Find"]' in drawn
    assert '    helper_lookup_gate_1{{"gate: approval for demo.record"}}' in drawn
    assert '  helper_lookup_gate_1 -.->|"declined or expired"| escalated' in drawn


@pytest.mark.parametrize(
    ("helpers", "tools", "message"),
    [
        (["ghost"], ["demo.read"], "helper 'ghost' is not a protocol"),
        ([], ["demo.missing"], "tool demo.missing is not in the workspace"),
    ],
)
def test_an_unresolvable_name_is_a_workspace_error(
    helpers: list[str], tools: list[str], message: str
) -> None:
    workspace, main = _workspace(helpers=helpers, tools=tools)
    with pytest.raises(WorkspaceError, match=message):
        compile_graph(workspace, main)


def test_an_unknown_protocol_exits_1() -> None:
    result = runner.invoke(app, ["graph", str(EXAMPLE), "--protocol", "nope"])
    assert result.exit_code == 1
    assert "no protocol 'nope'" in result.output


def _workspace(helpers: list[str], tools: list[str] | None = None) -> tuple[Workspace, Protocol]:
    main = Protocol(
        name="main",
        steps=[Step(number=1, name="Look", instruction="Use @demo.read.", tools=tools or [])],
        helpers=helpers,
    )
    lookup = Protocol(
        name="lookup",
        steps=[Step(number=1, name="Find", instruction="...", tools=["demo.record"])],
    )
    tool_set = [
        Tool(name="demo.read", kind="python", scope="read"),
        Tool(name="demo.record", kind="python", scope="write"),
    ]
    colleague = Colleague(
        name="c", role="r", escalation_contact="boss@example.com", protocols=["main"]
    )
    workspace = Workspace(
        name="w", colleagues=[colleague], protocols=[main, lookup], tools=tool_set
    )
    return workspace, main
