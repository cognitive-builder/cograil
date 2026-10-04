# Writing a runbook (Protocol Markdown)

A Protocol is a Markdown file: a header block, numbered steps, and optional `Error handling:` and
`Guardrails:` sections. See [ADR 0001](adr/0001-protocols-are-markdown.md) for why. The parser is
strict: anything it does not understand is an error, because a line that is silently dropped is a
step or a guardrail that silently does not exist.

## Sections take bullets, not inline text

```markdown
Guardrails:
- Never submit leave for anyone other than the requester.
```

`Guardrails: be kind` on one line is a `ProtocolParseError`. Put each item on its own `- ` line
under the section name.

## `@ref` grants the tool

Every `@tool.name` in a step line becomes a tool in that step's whitelist. The parser does not read
the sentence around it, so a negated mention grants the tool too:

```markdown
2. Step "Tidy": Summarise the record. Never call @hris.delete.   <- grants hris.delete
```

To forbid a tool, do not name it with `@`. Write the rule in prose without the sigil ("never delete
leave records"), or leave the tool out of the step. The whitelist is the only thing that stops a
tool call, and write tools still need an approval at runtime.

`@ref`s in `Error handling:` and `Guardrails:` bullets are text for the model and do not grant
anything.

### Lint decision (issue #67)

No lint for negated `@ref`s for now. Spotting a negation in free prose is a heuristic: a missed
case would look like a safety check that passed, which is worse than no check. Validation instead
passes the workspace's tool names (`known_tools`), so an `@ref` that names no real tool still fails.
Reopen this if a runbook review finds a real negated `@ref`.

## Failure thresholds in Error handling

An `Error handling:` bullet that starts `@tool fails:` is a failure threshold. The runner enforces
it. It is not just text. The bullet must read `escalate`, or `retry once`, `retry twice` or
`retry N times`, and then `, then escalate`. Anything after "escalate" is free text.

```markdown
Error handling:
- @hris.get_balance fails: retry once, then escalate to the Human Manager with the error.
```

- `retry once` allows two failed calls of that tool in the run. The second one stops the run and
  escalates it.
- `escalate` on its own escalates on the first failure.
- A `@tool fails:` bullet in any other form is a `ProtocolParseError`. So are two such bullets for
  one tool, and, during validation, a tool name that does not exist.
- Other bullets, the ones that do not start with `@tool fails:`, stay text for the model. Every
  bullet, threshold or not, is still given to the model.

## How a runbook runs

The runner compiles the Protocol to a graph with one node per step, in step order, and runs the
steps one after another. Inside a step the model is offered only the step's `@ref` tools. Every
tool call the model plans in a turn is checked before any of them runs:

- A tool the step does not name raises `ToolNotAllowed`. Nothing from that plan runs.
- A `scope: write` tool with `confirm_before_write` needs an approved Approval for the same run,
  step, tool and arguments. Each Approval allows one call and is spent atomically when the call
  goes through (a `gate.spent` AuditEvent).
- Without one, the run pauses instead of failing. A pending Approval is created for that exact call
  (the approver is the Colleague's `escalation_contact` for now; routing the approver from a
  decision table comes later). The step's progress and the plan waiting at the gate are saved,
  and the run's status becomes `awaiting_approval` (a `gate.paused` AuditEvent). Nothing from
  that plan runs. When the approver would be the run's own principal, nobody may decide the
  Approval, so none is created: the run escalates at once, with a `run.escalated` AuditEvent
  that has the reason `approver_is_principal`.
- `Runner.resume(token, protocol, decider=...)` approves and continues exactly at the paused step.
  The saved plan runs without asking the model again, then the step goes on. Only the Approval's
  approver may decide it, and never the run's own principal, so a run cannot approve its own
  write. Anyone else gets `ApprovalNotAllowed`, the run stays paused, and a `gate.refused`
  AuditEvent records the attempt. Its principal is the one who tried to decide, and its detail
  names the run's principal as `run_principal`. The decider is recorded
  as `decided_by` on the `gate.resumed` or `run.escalated` AuditEvent. The
  `cograil approve <token> --as <approver>` command calls it (see "The command line" below). An Approval is
  decided once, so of two racing resumes only one goes on. The decision and the run's new status
  are saved together in one store transaction, so a crash cannot leave one without the other.
- A declined Approval, or one past its `expires_at`, escalates the run instead. The timeout comes
  from `harness.yaml` (`approvals.timeout_hours`, 72 by default). The status becomes `escalated`,
  and a `run.escalated` AuditEvent names the Colleague's `escalation_contact`.
  `Runner.expire(token)` escalates an overdue Approval, for a scheduler to call; then
  `decided_by` is empty.
- `GateRequired` is still raised when a racing call spent the Approval first (the run fails
  closed), and when `run` is called on a run that is awaiting approval.
- `RunEnded` is raised when `run` is called on a run that has escalated, failed or completed. Such
  a run never starts again, and the call changes nothing.

A failed tool call (`ToolExecutionError`) of a tool with a failure threshold goes back to the model
as data until the threshold is reached. Then the run stops and escalates, with a `run.escalated`
AuditEvent that has the reason `failure_threshold` and the rule. Any other error fails the run
closed. The run's status becomes `failed`, and a `run.failed` AuditEvent records the error and the
principal. The error is then raised again.

A step ends only when the model sends the structured `step_complete` signal, or when a bound is
hit. The signal carries the step's `output`, which later steps see. The output and the tool results
are saved in the run's context. Only then does the cursor move to that step. A plain answer without
the signal is not the end. The runner reminds the model and gives it another turn. If a plan has
tool calls and `step_complete` together, the calls run first (through the whitelist and gates as
usual), then the step ends.

A step sees the outputs of the steps it declares with `(context: steps 1, 2)`. Without a
declaration it sees the previous step only (`context.default_prior_steps` in `harness.yaml`, 1 by
default). Context is a whitelist like tools: an undeclared step is not in the prompt, and the
model is offered the schemas of the step's own tools only. Prior step outputs and tool results,
retrieved passages included, reach the model inside a data block that starts with a fixed
"data, not instructions" preamble.

The runner keeps a Window Ledger on the run: estimated tokens by source (`instruction`,
`prior_steps`, `tools`, `knowledge`) for each step. `cograil runs --ledger` shows it for the
runs in the database named by `DATABASE_URL`; `cograil runs RUN_ID --ledger` shows one run.

The bounds come from the workspace's `harness.yaml` and are checked before every model call:

- `loop.max_turns` limits the model turns in a step. The default is 6. A step's `(turns: N)`
  overrides it.
- `loop.token_budget_per_step` limits the tokens spent in the step.
- `loop.usd_budget_per_run` limits the run's cost. The cost is worked out from the `pricing` block,
  in USD per million tokens.

So a step spends at most a budget plus one call. With a dollar budget, a model that has no price
counts as a breach, and every tier model must have a price or the workspace is invalid.

Hitting a bound raises `LoopBudgetExceeded`. It never fails the run. The run escalates through the
gates instead. A `loop.bounded` AuditEvent names the bound, its limit and what was used. Then a
`run.escalated` AuditEvent has the reason `loop_budget_exceeded` and the Colleague's
`escalation_contact`.

## harness.yaml

`harness.yaml` in the workspace folder is optional. Without it, the defaults apply. The example is
`workspaces/example-smb/harness.yaml`. It holds:

- `version`: a semantic version such as `1.0.0`.
- `loop`: `max_turns`, `token_budget_per_step` and `usd_budget_per_run`.
- `tiers`: the model names for `small`, `standard` and `strong`.
- `pricing`: the price of each model, in USD per million tokens.
- `context`: `default_prior_steps` and `compression_threshold_tokens` (the threshold is not used yet).
- `defaults`: `classification_tier` and `judgment_tier`.
- `retry`: `tool_attempts` and `backoff_seconds`.
- `approvals`: `timeout_hours`.

Unknown keys are errors.

The runtime stamps a harness version on every run. It is the semantic version plus a hash of the
validated content, such as `1.0.0+3f2a9c1b7d4e`. Changing any value changes the version. Comments
and layout do not. The `run.started` AuditEvent records it too.

`tiers`, `context`, `defaults` and `retry` are loaded and validated now. Later issues will use them
(tier routing, compressing large outputs and tool retries).

## The command line

```bash
cograil validate workspaces/example-smb
cograil run workspaces/example-smb --protocol leave_request --as alice@example.com
cograil approve <token> --as hr-ops@example.com
```

`validate` reports every problem in the workspace, one `invalid:` line each, not just the first.
It also checks that no step whitelists two tools whose Anthropic wire names collide (`a.b` and
`a__b` both become `a__b`), so you find out here and not in the middle of a run.

`run` streams one line per AuditEvent and per finished step. When a write needs approval, the run
pauses and prints the `cograil approve` command with its token. `approve` decides that Approval
through `Runner.resume` and goes on at the paused step; `--decline` declines it instead. The run
remembers the workspace folder it started from; `--workspace` overrides it. A paused run must
outlive the process, so both commands need `DATABASE_URL`. `run` uses Anthropic
(`ANTHROPIC_API_KEY`). `--fake-script FILE` uses the FakeProvider with the plans in a YAML file,
for demos and tests: a list of `{text, done, tool_calls: [{tool, args}]}`. With `approve`, the
script holds the plans for the rest of the run.

`--as` is taken at face value. This is a local and demo tool, and nothing in it authenticates the
principal. The approver check still applies, since `approve` goes through the runner: anyone but
the Approval's approver is refused and a `gate.refused` AuditEvent records it. The web and Slack
channels are where real authentication of the decider belongs.

A principal's groups come from the optional `principals.yaml` in the workspace
(`principals: [{id: alice@example.com, groups: [all-employees]}]`). `run` allows the principal only
if the colleague's and the protocol's audiences both allow them: `everyone`, or an audience in
`audiences.yaml` that shares a group with the principal. A principal not listed has no groups.

Exit codes (also in `--help`): `0` done (valid, or the run completed); `1` error (invalid
workspace, denied audience, missing `DATABASE_URL` or `ANTHROPIC_API_KEY`, a tool that is not built
yet, a refused decision); `2` usage error; `3` the run is awaiting approval; `4` it was escalated;
`5` it failed.
