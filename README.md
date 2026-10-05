# Cograil

### AI colleagues on process rails

Cograil turns a Markdown runbook into a working AI colleague. Write the procedure the way you would hand it to a new hire; Cograil makes the model follow it step by step, with a tool whitelist on every step, a confirmation gate on every write, audience checks on who may ask, and an audit trail of everything that happened.

A conventional locomotive relies on wheel adhesion and slips on steep grades. A cog engine engages a rack rail and cannot slip or roll back. The model is the engine. The protocol is the rail. The gates are the ratchet.

> Status: pre-release, built in the open during October 2026. See `docs/Product Plan.md` for the roadmap and `docs/adr/` for the decisions.

## What a colleague looks like

```markdown
Protocol: Leave Request
Audience: all-employees
Manual execution: allowed

1. Step "Check balance": Use @hris.get_balance for the requester. Tell them the balance.
2. Step "Confirm": Ask the requester to confirm the dates and leave type.
3. Step "Submit": Use @hris.submit_leave with the confirmed dates. (write; requires approval)
4. Step "Notify": Use @notify.send to tell the requester the outcome.

Error handling:
- @hris.get_balance fails: retry once, then escalate to the Human Manager.
```

The runtime parses the steps, restricts each step to the tools it names, pauses before any write for approval, and records every call.

## Quickstart (v0.1 target)

```bash
uv sync
uv run cograil validate workspaces/example-smb
uv run cograil run workspaces/example-smb --protocol leave_request --as alice@example.com --message "Annual leave from 2026-11-02 to 2026-11-04, please"
# a paused run prints: cograil approve <token> --as <approver>
```

`workspaces/example-enterprise` is a second, larger pack: Entra ID sign-in, two colleagues, SharePoint-style knowledge and a Jira tool over REST (see its `README.md`).

`run` and `approve` need `DATABASE_URL` (and `ANTHROPIC_API_KEY`, or `--fake-script`). They are a local and demo tool: `--as` is not authenticated. See `docs/runbooks.md`, "The command line".

## Design decisions

- Protocols are Markdown, versioned by git, readable by the business owner.
- Gates are tool metadata enforced by the runner, not instructions the model may ignore.
- Entitlement is applied before retrieval, never after.
- Workspace packs are data; the runtime is code. One runtime, many clients.
- Postgres is the only dependency for small deployments; Temporal is an optional backend for long-running approvals.
- Each step declares what it sees, how many turns it may take, and which model tier runs it; the harness that enforces this is versioned and stamped on every run.
- Decisions that matter are tables, not prompts: the model supplies inputs, the table decides, the audit log records the rule.

## How the backlog gets built

Work is GitHub issues. An autopilot on the maintainer's Mac starts one task at a time: Claude cloud sessions for lanes 1 and 2, and a GitHub Actions run on GLM for lane 3. Each session follows the same implement-issue command, GitHub merges green pull requests on its own, and a person steps in only for tasks labelled `needs:human`. See `docs/adr/0015-autopilot.md`.

## Limits (today)

No Teams adapter yet, no multi-tenant mode, no visual editor, no Temporal backend before v0.5. Knowledge corpora are expected to be small. See the Product Plan for what is coming and in what order.

## Licence

Apache-2.0
