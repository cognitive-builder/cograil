# Evals

An eval runs a workspace's golden set. The golden set is a list of cases. Each case is a real Run
that is checked against what you expect. `cograil eval` runs the cases and reports the outcome,
the tokens and the cost of each one. See [ADR 0013](adr/0013-runtime-cost-discipline.md) for why.

## What An Eval Case Is

A golden set is a JSONL file (JSON Lines): one JSON object per line, one case per line. Blank
lines are skipped. Every key is checked, and an unknown key is an error. Two cases may not share
an `id`.

- `id`, `protocol`, `principal`, `prompt`: the case's name, the Protocol to run, who asks, and
  what they ask.
- `smoke`: `true` puts the case in the smoke subset (see "The Three Levels").
- `scripted_only`: `true` for a case where the model misbehaves on purpose, for example it calls
  a Tool its Step does not list. Only the FakeProvider can play that model, so the live levels
  leave the case out.
- `script`: the FakeProvider's plans, in order. Each plan has `text`, `done` (adds the
  `step_complete` signal), `confidence` and `tool_calls`, a list of `{tool, args}`. The live
  levels ignore it.
- `decisions`: `approved` or `declined`, one for each gate the Run meets. They are applied in
  order, and each one is made by that gate's approver.
- `expect`: what the Run must do.
    - `status`: the status the Run ends in. It is a RunStatus, or `denied` when the audience
      check refuses the principal before any Run starts.
    - `error`: the typed error the Run failed with, such as `ToolNotAllowed`.
    - `tool_calls`: a list of `{tool, args, step}`.
    - `gates`: a list of `{tool, args, step}`, the Approvals the Run asked for.

Expected `args` are key args. Only the args you name are compared, so an id the tool makes up
does not break the case. Tool calls and gates are compared in order and in number. A `step` is
compared only if you give one.

Here is a case from the golden set. Its script plays one plan per turn: a search in Step 1,
then `step_complete` for each of the three Steps. The Run completes with one tool call and no
gate:

```json
{"id": "policy-annual-days", "protocol": "policy_question", "principal": "alice@example.com", "smoke": true, "prompt": "How many days of annual leave do I get a year?", "script": [{"tool_calls": [{"tool": "knowledge.search", "args": {"query": "annual leave days per year"}}]}, {"text": "Found the passage.", "done": true}, {"text": "\"Full-time employees accrue 20 days of annual leave per year.\" (Leave Policy, 1. Annual leave)", "done": true}, {"text": "No exception or approval asked; nothing to escalate.", "done": true}], "expect": {"status": "completed", "tool_calls": [{"tool": "knowledge.search", "step": 1}]}}
```

## The Golden Set For The Example Workspace

The golden set for `workspaces/example-smb` is `tests/evals/example-smb.jsonl`. It has 25 cases.
`cograil eval <workspace>` reads `tests/evals/<workspace folder name>.jsonl` from the current
directory. Use `--cases FILE` to read another file.

## Running It

```bash
cograil eval workspaces/example-smb
COGRAIL_LIVE=1 cograil eval workspaces/example-smb
cograil eval workspaces/example-smb --cases my-cases.jsonl
```

The first line runs the `fake` level. The second runs the `smoke` level on a real model. The
third reads cases from your own file. `--level fake|smoke|full` picks a level.

Each case runs a real Run through the Runner. The RunStore is in memory, so `eval` needs no
`DATABASE_URL`. The workspace's tools are built fresh for each case. Its knowledge is synced
into an in-memory store. Its harness bounds and prices apply.

## The Three Levels

The levels run from free to dear (ADR 0013).

- `fake` is the default. Every case runs on the FakeProvider, which replays the case's script.
  It costs $0, because the fake model is priced at zero. The report still shows the tokens. The
  unit suite runs this level on every pull request, through `tests/evals/test_golden_set.py`.
- `smoke` needs `COGRAIL_LIVE=1` and `ANTHROPIC_API_KEY`. It runs the cases marked `smoke`,
  without the `scripted_only` ones, and maps every tier to the small tier's model. Run it on
  demand.
- `full` needs `COGRAIL_LIVE=1`. It runs every case a real model can play, on the workspace's own
  tiers. It is for releases only, and only through the provider's batch path. That path does not
  exist yet (issue #55), so until it does, `full` refuses to run.

Without `COGRAIL_LIVE=1`, asking for `smoke` is refused, so a run on a real model never starts
by accident. `full` is refused either way until the batch path exists.

## The Report

The report has one line per case: PASS or FAIL, the Run's status, the tokens (in, out, cache
read and cache write) and the cost. Each mismatch is listed under its failing case.

Then comes one line per Protocol:

- `cases`: how many cases ran.
- `passed`: how many matched their expectation.
- `resolved`: how many Runs completed without escalation.
- `tokens` and `cost`: the totals for the Protocol.
- `cost/resolved`: the cost per resolved run. It is the total cost of the Runs that completed
  without escalation, divided by their count (ADR 0013). It shows `-` when none resolved.

The last line is the tally, `passed N/M`.

Exit codes: `0` every case passed. `1` a case failed, or an error (an invalid workspace or case
file, a live level without `COGRAIL_LIVE=1`, the `full` level, a missing `ANTHROPIC_API_KEY`).
`2` usage error.
