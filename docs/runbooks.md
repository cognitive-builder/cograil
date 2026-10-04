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
  (the approver is the run's principal for now). The step's progress and the plan waiting at the
  gate are saved, and the run's status becomes `awaiting_approval` (a `gate.paused` AuditEvent).
  Nothing from that plan runs.
- `Runner.resume(token, protocol)` approves and continues exactly at the paused step. The saved
  plan runs without asking the model again, then the step goes on. The `cograil approve <token>`
  command that calls it comes with the CLI issue (#13). An Approval is decided once, so of two
  racing resumes only one goes on.
- A declined Approval, or one past its `expires_at` (72 hours by default), escalates the run
  instead. The status becomes `escalated`, and a `run.escalated` AuditEvent names the Colleague's
  `escalation_contact`. `Runner.expire(token)` escalates an overdue Approval, for a scheduler to
  call.
- `GateRequired` is still raised when a racing call spent the Approval first (the run fails
  closed), and when `run` is called on a run that is awaiting approval.

A failed tool call (`ToolExecutionError`) of a tool with a failure threshold goes back to the model
as data until the threshold is reached. Then the run stops and escalates, with a `run.escalated`
AuditEvent that has the reason `failure_threshold` and the rule. Any other error fails the run
closed. The run's status becomes `failed`, and a `run.failed` AuditEvent records the error and the
principal. The error is then raised again.

A step is complete when the model answers without calling a tool. Its text and tool results are
saved in the run's context. Only then does the cursor move to that step. A step sees the outputs of
the steps it declares with `(context: steps 1, 2)`. Without a declaration it sees only the
previous step. `(turns: N)` limits a step to N model turns, and the default is 6. A step that is
not complete after its last turn raises `LoopBudgetExceeded`.
