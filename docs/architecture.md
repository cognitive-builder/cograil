# Architecture

Cograil turns a Markdown runbook into an AI colleague that follows it step by step. The model
reasons inside a `Step`; the rails — steps, tool whitelists, gates, budgets, the audit trail —
are data and code around it. This page describes the code as it stands; each layer names the
module that implements it.

## Layers

```
+---------------------------------------------------------------------------+
| Workspace pack (data)                                          ADR 0004    |
| colleagues/*.yaml  protocols/*.md  tools.yaml  connections.yaml           |
| audiences.yaml  principals.yaml  knowledge.yaml  harness.yaml  tools/*.py |
+---------------------------------------------------------------------------+
      |  workspace.py load_workspace · parser.py parse_protocol
      |  validate.py validate_workspace · audience.py check_audience
+---------------------------------------------------------------------------+
| Entry point                                                               |
| cli.py: cograil run | approve | runs | validate                           |
+---------------------------------------------------------------------------+
      |
+---------------------------------------------------------------------------+
| Runner                                                       ADR 0011      |
| runner.py compile_protocol: a LangGraph graph, one node per Step          |
|                                                                           |
|   context.py   what a Step sees; the Window Ledger           ADR 0007     |
|   harness.py   version, loop bounds, cost                 ADR 0008, 0012  |
|   gates.py     Approvals, escalation                        ADR 0002     |
|   providers/   one Provider interface; Anthropic default    ADR 0005     |
|   registry.py  Tool kinds: python, rest, mcp (tool_kinds/)                |
+---------------------------------------------------------------------------+
      |
+---------------------------------------------------------------------------+
| State                                                        ADR 0003      |
| store.py RunStore -> PostgresRunStore (default), InMemoryRunStore (tests) |
| runs, tool_calls, approvals, audit_events (append-only)                   |
+---------------------------------------------------------------------------+
```

The workspace pack is data the runtime loads, never code the runtime forks per client. The
runner owns every check: the Provider answers "what does the model want to do next", the
registry answers "what is this Tool and what did it do", and neither decides whether a call
may happen. State lives in one place, behind the `RunStore` interface.

## Request path

A Run arrives on the CLI. The web and Slack channels are not built yet (issues #17, #18, #27); when
they are, they enter at the same place the CLI does: a loaded workspace, a provider, a store,
a `Runner`.

1. `cograil run workspaces/example-smb --protocol leave_request --as alice@example.com`.
2. `load_workspace` reads the pack. Every failure is a `WorkspaceError` naming the file;
   `validate_workspace` reports every problem at once rather than the first.
3. The Protocol is picked by name, then the Colleague whose `protocols` list names it.
4. The principal comes from `principals.yaml`. An unknown `--as` still runs, with no groups:
   the CLI is a local and demo tool and does not authenticate anyone. See `docs/auth.md` for
   how the web service signs people in.
5. `check_audience` runs before the Run exists. An `AudienceDenied` exits 1: entitlement is
   checked before anything is spent.
6. The Provider is `FakeProvider` with the plans from `--fake-script`, or
   `AnthropicProvider.for_protocol` (which needs `ANTHROPIC_API_KEY`).
7. `open_store` opens the `PostgresRunStore` named by `DATABASE_URL`, wrapped in a
   `ProgressStore` that echoes every AuditEvent and every finished Step as a line of progress.
8. `build_registry` builds the `python`, `rest` and `mcp` kinds. A Step that names a Tool
   nothing implements refuses to start: the Run never begins with a whitelist it cannot honour.
9. The Run is created — `Trigger` kind `chat`, channel `cli` — and saved.
10. `Runner.run` stamps the harness version on the Run, writes `run.started`, and
    `compile_protocol` builds the graph: one node per Step, edges in step order, entry at the
    Step after `Run.cursor`.
11. Inside a Step, until `step_complete` or a bound:
    - `ContextBuilder.opening` builds what the model sees: the prior Steps the Step declares
      (else the harness default), the schemas of its whitelisted Tools only, and the data
      preamble. Its tokens are counted in the Window Ledger, kept in `Run.context["ledger"]`.
    - `check_bounds` runs before every provider call: the Step's turns, its token budget, the
      Run's cost budget. A breach escalates the Run.
    - `Provider.plan` returns text and planned tool calls, with token usage.
    - Every call of the plan is checked before any of them runs. A Tool the Step does not
      whitelist raises `ToolNotAllowed` and nothing runs.
    - A write Tool with `confirm_before_write` needs an approved Approval for the same Run,
      step, Tool and arguments. Without one the Run does not fail — `Gates.pause` saves the
      Step's progress in `Run.context["paused"]`, creates a pending Approval, and the Run goes
      `awaiting_approval`. The CLI prints the `cograil approve` command and exits 3.
    - `ToolRegistry.invoke` validates the arguments against the Tool's `args_schema`, calls it,
      and records a `ToolCall` and a `tool.called` AuditEvent — plus `tool.started` just before
      a write, so a crash mid-write still leaves a record.
    - Results go back to the model as data, inside the data block. A failed call counts
      against the Tool's threshold from the Protocol's `Error handling:` section and escalates
      the Run at the limit; a Tool with no threshold fails the Run closed.
    - The structured `step_complete` signal ends the Step. Its output and tool calls are saved
      to `Run.context["steps"]`, and `Run.cursor` moves to the Step's number.
12. The last Step done, the Run completes and `run.completed` is written. Any error fails the
    Run closed: status `failed`, a `run.failed` AuditEvent, the cursor left at the last
    completed Step.

A paused Run continues with `cograil approve <token> --as <approver>`, which calls
`Runner.resume`. Only the Approval's approver may decide it, never the Run's own principal;
anyone else is refused and the attempt is audited. Two racing resumes cannot both succeed:
the Approval is decided once, and the decision and the Run are saved in the same transaction.
Approved, the Run continues exactly at the paused Step and runs the plan that was waiting.
Declined or past its expiry, it escalates to the Colleague's escalation contact. A Protocol
whose version has changed since the Run started is refused.

Every gate, approval, tool call and completion is an `AuditEvent` with the principal. The
`audit_events` table is append-only: the store offers `append_audit_event` and nothing that
edits, and a database trigger rejects UPDATE, DELETE and TRUNCATE.

## Intent classification

`orchestrator.py` decides which Colleague and Protocol a free-text message is for, before any
Run exists. `classify_intent` offers the model one read tool, `route`, whose `choice` is an
enum of the `colleague/protocol` pairs the principal may start, plus `none`; the model cannot
name anything else. A choice outside the list, a missing or out-of-range confidence, or no
`route` call at all routes to `none`. `none` returns a refusal (`Routing.refusal`) listing what
the principal can do and the escalation contacts of those Colleagues. The classification model
is the harness's `classification_tier` (`claude-haiku-4-5` by default); build the Provider on
`classification_model(workspace)`. Each classification is logged as `orchestrator.classified`
with the confidence, so evals can replay it.

Audiences are a pre-filter (issue #20): the enum and the refusal are built from the same
filtered list, so a Protocol outside the principal's audiences is never offered to the model
nor named in the refusal, and asking for it ends in a refusal, not an error. The rule lives in
`audience.py` (`audience_denial`, and `check_audience` which raises `AudienceDenied`), shared
with `cograil run`: for a user, the Colleague's and the Protocol's audiences must both allow
the principal and the Protocol must allow manual execution; the system actor (a `Principal`
of kind `system`) is allowed exactly when the Protocol allows scheduled execution.

## Not built yet

- The web and Slack channels, and the API in front of them (issues #17, #18, #27). The CLI is the only
  entry point, and `--as` is not authenticated.
- The `directory` Tool kind. `build_registry` builds `python`, `rest`, `mcp` and `decision`. The
  `knowledge` kind registers from `knowledge/tool.py` with its ACL pre-filter (issue #23).
- OpenTelemetry spans. `observability.py` writes structured JSON log lines today.
- Approver routing: decision tables exist now, and `approval_routing` returns an approver
  tier. The approver of a gate is still the Colleague's escalation contact until a directory
  lookup maps a tier to a principal.
