"""Compile a Protocol to its explicit graph and render it as Mermaid (ADR 0011).

The graph has one node per Step with edges in step order, a gate node after every Step that
whitelists a Tool needing approval, a subgraph for each helper Protocol, and an edge to
`escalated` wherever the Protocol declares a failure threshold or a gate can be declined. The
model never adds or removes a node or an edge. No Decision table routes the Runner today, so
a Decision Tool only shows in its Step's label. The Runner still builds its own LangGraph graph
(`runner.compile_protocol`); this module is the reviewable picture of the same shape.
"""

from __future__ import annotations

import re
from typing import Literal

from cograil.domain import Entity, Protocol, Step, Tool, Workspace
from cograil.errors import WorkspaceError
from cograil.gates import needs_approval

START, DONE, ESCALATED = "start", "done", "escalated"
NodeKind = Literal["start", "step", "gate", "end"]
EdgeKind = Literal["next", "gate", "error"]


class GraphNode(Entity):
    id: str
    label: str
    kind: NodeKind
    subgraph: str | None = None  # the helper Protocol this node belongs to


class GraphEdge(Entity):
    source: str
    target: str
    kind: EdgeKind
    label: str = ""


class ProtocolGraph(Entity):
    protocol: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def compile_graph(workspace: Workspace, protocol: Protocol) -> ProtocolGraph:
    """The graph of `protocol`, with each helper Protocol it names as a subgraph.

    Raises WorkspaceError when a helper names no Protocol or a Step names no Tool.
    """
    tools = {tool.name: tool for tool in workspace.tools}
    nodes = [
        GraphNode(id=START, label="start", kind="start"),
        GraphNode(id=DONE, label="completed", kind="end"),
    ]
    edges: list[GraphEdge] = []
    entry, last = _body(protocol, tools, "", None, nodes, edges)
    edges.insert(0, GraphEdge(source=START, target=entry, kind="next"))
    edges.append(GraphEdge(source=last, target=DONE, kind="next"))
    for name in protocol.helpers:
        helper = next((p for p in workspace.protocols if p.name == name), None)
        if helper is None:
            raise WorkspaceError(f"protocol {protocol.name}: helper {name!r} is not a protocol")
        _body(helper, tools, f"helper_{_slug(name)}_", name, nodes, edges)
    if any(edge.target == ESCALATED for edge in edges):
        nodes.insert(2, GraphNode(id=ESCALATED, label="escalated", kind="end"))
    return ProtocolGraph(protocol=protocol.name, nodes=nodes, edges=edges)


def _body(
    protocol: Protocol,
    tools: dict[str, Tool],
    prefix: str,
    subgraph: str | None,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> tuple[str, str]:
    """Add one Protocol's Steps, gates and error edges; return its entry and exit node ids."""
    if not protocol.steps:
        raise WorkspaceError(f"protocol {protocol.name} has no steps")
    previous: str | None = None
    entry = ""
    for step in protocol.steps:
        step_tools = [_tool(tools, protocol, step, name) for name in step.tools]
        node = f"{prefix}step_{step.number}"
        nodes.append(GraphNode(id=node, label=_step_label(step, step_tools), kind="step",
                               subgraph=subgraph))  # fmt: skip
        if previous is None:
            entry = node
        else:
            edges.append(GraphEdge(source=previous, target=node, kind="next"))
        previous = _gate(step, step_tools, node, prefix, subgraph, nodes, edges)
        edges.extend(_errors(protocol, step, node))
    assert previous is not None
    return entry, previous


def _tool(tools: dict[str, Tool], protocol: Protocol, step: Step, name: str) -> Tool:
    try:
        return tools[name]
    except KeyError:
        raise WorkspaceError(
            f"protocol {protocol.name} step {step.number}: tool {name} is not in the workspace"
        ) from None


def _gate(
    step: Step,
    step_tools: list[Tool],
    node: str,
    prefix: str,
    subgraph: str | None,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> str:
    """Add the gate interrupt after a Step that has gated Tools; return where the flow goes on."""
    gated = [tool.name for tool in step_tools if needs_approval(tool)]
    if not gated:
        return node
    gate = f"{prefix}gate_{step.number}"
    nodes.append(GraphNode(id=gate, label="gate: approval for " + ", ".join(gated), kind="gate",
                           subgraph=subgraph))  # fmt: skip
    edges.append(GraphEdge(source=node, target=gate, kind="gate", label="write"))
    edges.append(GraphEdge(source=gate, target=ESCALATED, kind="error",
                           label="declined or expired"))  # fmt: skip
    return gate


def _errors(protocol: Protocol, step: Step, node: str) -> list[GraphEdge]:
    """One edge to `escalated` per declared failure threshold on a Tool the Step whitelists."""
    return [
        GraphEdge(
            source=node,
            target=ESCALATED,
            kind="error",
            label=f"{threshold.tool} fails {threshold.max_failures}x",
        )
        for threshold in protocol.failure_thresholds
        if threshold.tool in step.tools
    ]


def _step_label(step: Step, step_tools: list[Tool]) -> str:
    lines = [f"{step.number}. {step.name}"]
    bounds: list[str] = [step.model_tier] if step.model_tier else []
    if step.max_turns is not None:
        bounds.append(f"max {step.max_turns} turns")
    if bounds:
        lines.append(", ".join(bounds))
    decisions = [tool.name for tool in step_tools if tool.kind == "decision"]
    if decisions:
        lines.append("decision: " + ", ".join(decisions))
    return "\n".join(lines)


def _slug(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_")


_SHAPES = {"start": "(({}))", "step": '["{}"]', "gate": '{{{{"{}"}}}}', "end": '(["{}"])'}


def _text(label: str) -> str:
    return label.replace('"', "#quot;").replace("\n", "<br/>")


def _node_line(node: GraphNode, indent: str) -> str:
    shape = _SHAPES[node.kind].format(_text(node.label))
    return f"{indent}{node.id}{shape}"


def _edge_line(edge: GraphEdge) -> str:
    arrow = "-.->" if edge.kind == "error" else "-->"
    label = f'|"{_text(edge.label)}"|' if edge.label else ""
    return f"  {edge.source} {arrow}{label} {edge.target}"


def render_mermaid(graph: ProtocolGraph) -> str:
    """A Mermaid flowchart: solid arrows are the Step order, dotted arrows lead to `escalated`."""
    lines = ["flowchart TD"]
    lines += [_node_line(n, "  ") for n in graph.nodes if n.subgraph is None]
    for name in dict.fromkeys(n.subgraph for n in graph.nodes if n.subgraph is not None):
        lines.append(f'  subgraph helper_{_slug(name)}["helper: {_text(name)}"]')
        lines += [_node_line(n, "    ") for n in graph.nodes if n.subgraph == name]
        lines.append("  end")
    lines += [_edge_line(edge) for edge in graph.edges]
    return "\n".join(lines) + "\n"
