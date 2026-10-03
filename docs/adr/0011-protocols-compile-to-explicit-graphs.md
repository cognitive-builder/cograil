# ADR 0011: Protocols Compile To Explicit Graphs

Status: Proposed · Date: 2026-10-03 · Discipline: Graph Engineering

## Context

The runner is a LangGraph graph already; the decision is what the graph is allowed to be.

## Decision

A Protocol compiles to a graph with one node per Step, deterministic edges in step order, helper protocols as subgraphs, gates as interrupts, and conditional edges only where a decision table or a declared error-handling rule produces them. The model never adds or removes nodes or edges. `cograil graph <protocol>` renders the compiled graph as Mermaid for review and documentation. Step directives use a single trailing parenthesised form: `(context: steps 1, 2; model: small; turns: 4)`.

## Consequences

The shape of the work is reviewable by someone who cannot read code. Graph RAG over knowledge and the Context Graph of decision traces are later decisions that build on the same principle: structure is explicit, the model operates inside it.
