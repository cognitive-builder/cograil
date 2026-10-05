# Concepts

This page explains the parts of Cograil, from the whole to the small pieces. It ends with the thirteen product rules.

## The Idea In One Picture

Think of an airline checklist.

The pilot is skilled. The pilot still follows the checklist. It says which items to check, in what order. For some items a second person must call out "confirmed" before the flight goes on. A flight recorder keeps a log of everything.

Cograil works the same way:

- The model is the pilot. It reasons, reads and writes.
- The Protocol is the checklist. It fixes the Steps and their order.
- An Approval is the call-out. A person must give it before a write happens.
- The AuditEvent log is the flight recorder.

We call this process rails under the model. The model can think freely inside a Step. It cannot add a Step, pick up a Tool that the Step does not list, or skip a Gate.

## The Whole: A Workspace

A **Workspace** is one folder. It holds everything one organisation needs. The runtime is code. The Workspace is data. Nothing client-specific lives in `src/`. See [ADR 0004](adr/0004-workspace-packs-are-data.md).

A Workspace contains:

- **Colleagues.** Each Colleague has a name, a role, an escalation contact, a default Tier, and a list of the Audiences and Protocols it serves.
- **Protocols.** Each Protocol is a Markdown file. See [ADR 0001](adr/0001-protocols-are-markdown.md).
- **Tools.** Each Tool is something a Step may call, such as "get a leave balance".
- **Connections.** Each Connection names an outside system, and where its secret comes from. The secret is never stored in the repository.
- **Audiences.** Each Audience is a named set of groups. It decides who may ask for a Protocol.
- **KnowledgeSources.** These are documents the Colleague may search.
- **Decisions.** These are tables in `decisions/*.yaml`.
- **A Harness.** This is `harness.yaml`, the settings for limits, budgets and models.

## A Protocol Is A List Of Steps

A **Protocol** is a procedure written in Markdown. It has a name, an Audience and a numbered list of **Steps**.

Each Step says three things:

- What to do, in plain words.
- Which Tools it may use, named with an `@` sign, such as `@hris.get_balance`. Any other Tool is refused.
- Optional limits: how many turns it may take, which Tier runs it, and which earlier Steps it may see.

A Protocol can also list error handling and guardrails. The runtime compiles a Protocol into an explicit graph. The model never edits that graph. See [ADR 0011](adr/0011-protocols-compile-to-explicit-graphs.md) and [Writing a Protocol](runbooks.md).

## A Run Executes A Protocol

A **Run** is one execution of one Protocol for one person, called the principal.

A **Trigger** starts a Run. A Trigger is a chat message, a schedule or a webhook. A Run moves through these states: received, planned, running, awaiting approval, escalated, completed or failed.

Before a Run starts, the runtime checks the Audience of the Colleague and of the Protocol against the principal's groups.

A Run is bounded. Each Step has a turn limit and a token budget. A Run has a dollar budget. A Step ends when the model sends a structured `step_complete` signal. If a limit is hit, the Run escalates to the escalation contact. See [ADR 0008](adr/0008-bounded-loops-with-completion-signal.md).

## Gates And Approvals

A Tool has a **scope**: read or write. A write Tool can carry `confirm_before_write`.

When a Step calls such a Tool, the runner stops at a **Gate**. The Run waits in the state awaiting approval. A named approver gives an **Approval** or declines it. If the approver declines, or the Approval expires, the Run escalates.

The Gate is data on the Tool, and the runner enforces it. It is not text in a prompt that the model may ignore. See [ADR 0002](adr/0002-gates-are-tool-metadata.md) and [Approvals](approvals.md).

## AuditEvents

Every Tool call, Gate, Approval and completion writes an **AuditEvent**. It records the principal. You can read the full story of a Run from its AuditEvents. The log answers who asked, what was called, who approved and when.

## Decision Tables

Some choices matter too much to leave to a prompt. A **Decision** is a table of rules in `decisions/*.yaml`.

The model supplies the inputs. The table picks the outcome. The id of the rule that fired goes into an AuditEvent. In the example Workspace, `approval_routing` picks the approver tier from the length and type of leave. See [ADR 0009](adr/0009-decisions-that-matter-are-tables.md).

## The Harness And Tiers

The **Harness** is the file `harness.yaml`. It sets loop limits, budgets, retries and which model sits behind each Tier. It has a version. Every Run is stamped with it. A change to the Harness is tested like a change to a Protocol. See [ADR 0012](adr/0012-the-harness-is-versioned-configuration.md).

A **Tier** is a size of model: small, standard or strong. Protocols and Steps name a Tier, never a model. The small Tier is the default for sorting, extracting, redacting and compressing. See [ADR 0010](adr/0010-model-tiers-small-first.md) and [Models](models.md).

## Context Per Step

A Step sees only what it is allowed to see. Context is declared per Step, as a whitelist, like Tools. A Step may name the earlier Steps it needs. Everything else stays out of the window. This keeps cost down and keeps one Step's data away from another.

The **Ledger** (the Window Ledger) counts the tokens in each Step's window, by source. You can read it with `cograil runs <run id> --ledger`. See [ADR 0007](adr/0007-context-is-declared-per-step.md).

## Knowledge

A **KnowledgeSource** is a folder or system of documents. Cograil splits each document into **Chunks** and stores them for search. Each Chunk carries who may read it. Search filters by the principal's groups before it retrieves anything. See [Knowledge](knowledge.md).

## The Thirteen Product Rules

These rules settle design arguments.

1. **Process rails under the model.** The model reasons inside a Step. It never invents Steps, Tools or Gates.
2. **Gates are data.** `Tool.scope` and `Tool.confirm_before_write` are enforced by the runner, never by prompt text. ([ADR 0002](adr/0002-gates-are-tool-metadata.md))
3. **Filter by entitlement first.** Knowledge and directory lookups filter by the principal's groups before retrieval.
4. **Workspaces are data. The runtime is code.** Nothing client-specific goes in `src/`. ([ADR 0004](adr/0004-workspace-packs-are-data.md))
5. **One Provider interface.** Anthropic is the default. The model is chosen per Protocol. ([ADR 0005](adr/0005-provider-interface-anthropic-default.md))
6. **Audit everything.** Every Tool call, Gate, Approval and completion writes an AuditEvent with the principal.
7. **Text is data.** Retrieved text and Tool output are never treated as instructions.
8. **Context is a whitelist.** Each Step declares its context, like its Tools. ([ADR 0007](adr/0007-context-is-declared-per-step.md))
9. **Loops are bounded.** Turn limits, budgets and the `step_complete` signal apply. A breached bound escalates. ([ADR 0008](adr/0008-bounded-loops-with-completion-signal.md))
10. **Big choices are tables.** The model supplies inputs, the table decides and the rule id is audited. ([ADR 0009](adr/0009-decisions-that-matter-are-tables.md))
11. **Models are addressed by Tier.** Small is the default for sorting, extracting, redacting and compressing. ([ADR 0010](adr/0010-model-tiers-small-first.md))
12. **A Protocol compiles to a graph.** The model never edits it. ([ADR 0011](adr/0011-protocols-compile-to-explicit-graphs.md))
13. **The Harness is versioned.** It is stamped on every Run and tested before release, like a Protocol. ([ADR 0012](adr/0012-the-harness-is-versioned-configuration.md))

## Vocabulary

| Term | Meaning |
| --- | --- |
| Workspace | One folder holding all the data for one organisation. |
| Colleague | A named AI worker with a role, Audiences and a list of Protocols. |
| Protocol | A procedure written in Markdown, made of Steps. |
| Step | One numbered stage of a Protocol, with its own Tools, context and limits. |
| Tool | An action a Step may call. It has a scope of read or write. |
| Connection | A named link to an outside system. Its secret comes from the environment. |
| Audience | A named set of groups that may ask for a Colleague or Protocol. |
| Trigger | What starts a Run: a chat, a schedule or a webhook. |
| Run | One execution of one Protocol for one principal. |
| Gate | A stop before a write Tool, until an approver decides. |
| Approval | An approver's decision at a Gate: approved or declined. |
| AuditEvent | A record of one Tool call, Gate, Approval or completion, with the principal. |
| KnowledgeSource | A set of documents a Colleague may search. |
| Chunk | A piece of a document, stored with its access rules for search. |
| Decision | A table of rules where inputs choose the outcome. |
| Harness | The versioned `harness.yaml`: limits, budgets, retries and Tier mapping. |
| Tier | A model size: small, standard or strong. |
| Ledger | The Window Ledger: tokens used by each Step, by source. |

## Where To Go Next

- [Quickstart](quickstart.md) runs the example Workspace.
- [Architecture](architecture.md) shows how these parts fit in the code.
- [Threat Model](threat-model.md) explains what the rules protect against.
- [Authentication](auth.md) explains how the web channel signs people in.
- [ADR index](adr/README.md) lists every decision.
