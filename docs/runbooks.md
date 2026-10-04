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

## How a runbook runs

The runner compiles the Protocol to a graph with one node per step, in step order, and runs the
steps one after another. Inside a step the model is offered only the step's `@ref` tools. Every
tool call the model plans in a turn is checked before any of them runs:

- A tool the step does not name raises `ToolNotAllowed`. Nothing from that plan runs.
- A `scope: write` tool with `confirm_before_write` needs an approved Approval for the same run,
  step, tool and arguments. Each Approval allows one call. Without one, `GateRequired` is raised.
  Pausing the run to ask for an Approval comes with the gates issue (#11).

Any error fails the run closed. The run's status becomes `failed`, and a `run.failed` AuditEvent
records the error and the principal. The error is then raised again.

A step is complete when the model answers without calling a tool. Its text and tool results are
saved in the run's context. Only then does the cursor move to that step. A step sees the outputs of
the steps it declares with `(context: steps 1, 2)`. Without a declaration it sees only the
previous step. `(turns: N)` limits a step to N model turns, and the default is 6. A step that is
not complete after its last turn raises `LoopBudgetExceeded`.
