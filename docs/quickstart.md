# Quickstart

This page takes you from a fresh checkout to a paused and approved Run. You will use the example Workspace in `workspaces/example-smb`. It is a small pack for an imaginary company. Its Colleague is Harper, an HR operations assistant.

You do not need an API key. A scripted stand-in for the model, called the FakeProvider, plays the model's part.

## Before You Start

You need:

- Python 3.12 and [uv](https://docs.astral.sh/uv/), the Python package manager.
- Docker, for a local Postgres database. Postgres holds Runs and Approvals so a paused Run can outlive the process.

## 1. Install

```bash
uv sync
```

## 2. Validate The Workspace

```bash
uv run cograil validate workspaces/example-smb
```

A good Workspace prints one line that starts with `ok:`. It names the Workspace and counts its Colleagues, Protocols and Tools.

A bad Workspace prints one `invalid:` line for every problem, not just the first. The exit code is 1.

## 3. Look At The Graph

```bash
uv run cograil graph workspaces/example-smb --protocol leave_request
```

This prints the Protocol as a Mermaid flowchart. Mermaid is a text format for diagrams. Paste the output into any Mermaid viewer.

You will see one node per Step. A Step that needs an Approval has a Gate node. The command starts no Run and needs no database or model.

## 4. Try A Decision Table

The `leave_request` Protocol uses a Decision table to choose who approves. You can test the table by hand:

```bash
uv run cograil decide approval_routing --workspace workspaces/example-smb \
  --input duration_days=12 --input leave_type=annual --input requester_role=staff
```

The output names the rule that fired. Here it is `r2`, because the leave is longer than 10 days. This is the rule id that a Run writes to its AuditEvent. This command needs no database or model.

## 5. Start The Database

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://cograil:cograil@localhost:5432/cograil
uv run alembic upgrade head
```

The URL matches the values in `docker-compose.yml`. The last command creates the tables. `run` and `approve` both need `DATABASE_URL`. See [Deploying](deploy.md) for the database roles a real server uses.

## 6. Write A Script For The FakeProvider

The repository has no ready-made script for the example Workspace, so write two small files. A script is a YAML list of plans. The FakeProvider hands them out in order, one per model turn. Each plan has `text`, an optional `done: true` that ends the Step, and an optional list of `tool_calls`.

Save this as `leave-first.yaml`. It covers everything up to the Gate.

```yaml
# Step 1: Check balance
- tool_calls: [{tool: hris.get_balance, args: {employee: alice}}]
- {text: "You have 25 annual days and 10 sick days.", done: true}
# Step 2: Confirm dates
- {text: "Confirmed: annual leave, 2026-11-02 to 2026-11-04.", done: true}
# Step 3: Route and submit (this Step reaches the Gate)
- tool_calls: [{tool: decide.approval_routing, args: {duration_days: 3, leave_type: annual, requester_role: staff}}]
- tool_calls: [{tool: hris.submit_leave, args: {employee: alice, start: "2026-11-02", end: "2026-11-04", leave_type: annual, request_id: req-1}}]
```

Save this as `leave-rest.yaml`. It covers what happens after the Approval.

```yaml
# Step 3 finishes after the write
- {text: "Submitted. Your manager must approve.", done: true}
# Step 4: Notify
- tool_calls: [{tool: notify.send, args: {to: alice@example.com, message: "Your leave request req-1 was submitted."}}]
- {text: "Alice has been told.", done: true}
```

If a script runs out of plans, the Run fails with a "script exhausted" error. If a Run behaves oddly, check that your plans match the Steps in `workspaces/example-smb/protocols/leave_request.md`.

## 7. Run The Protocol

```bash
uv run cograil run workspaces/example-smb --protocol leave_request \
  --as alice@example.com \
  --message "Annual leave from 2026-11-02 to 2026-11-04, please" \
  --fake-script leave-first.yaml
```

`run` prints one line for each AuditEvent and each finished Step. The Tool `hris.submit_leave` is a write Tool with `confirm_before_write: true`. The runner stops at the Gate before the Tool runs.

The command ends with a line like this:

```text
awaiting hr-ops@example.com to approve hris.submit_leave (step 3): cograil approve <token> --as hr-ops@example.com
```

The exit code is 3, which means awaiting approval. Today the approver is the Colleague's `escalation_contact`. In this Workspace that is set in `colleagues/harper.yaml`. The Run's own principal can never approve it.

## 8. Approve

Copy the token from the line above.

```bash
uv run cograil approve <token> --as hr-ops@example.com --fake-script leave-rest.yaml
```

The Run continues at the paused Step, submits the leave and runs the last Step. The exit code is 0.

To decline instead, add `--decline`. A declined Approval escalates the Run to the escalation contact. Only the approver may decide. If anyone else tries, the command is refused and a `gate.refused` AuditEvent records the attempt.

A note on `--as`: the command line takes it at face value. It is a local and demo tool and it does not sign anyone in. Real sign-in belongs to the web and Slack channels. See [Approvals](approvals.md).

## 9. Look At What Happened

```bash
uv run cograil runs
uv run cograil runs <run id> --ledger
```

The first command lists the newest Runs with their status and cost. The second shows one Run with its Window Ledger, which counts the tokens each Step used, by source.

## Using A Real Model

Leave out `--fake-script` and set your key:

```bash
export ANTHROPIC_API_KEY=your-key-here
```

Without the key, `run` stops with a clear message. The Harness in `workspaces/example-smb/harness.yaml` maps each Tier (small, standard and strong) to a model name. See [Models](models.md).

## Next Steps

- [Concepts](concepts.md) explains the words used above.
- [Writing a Protocol](runbooks.md) shows how to write your own Protocol, and lists every command and exit code.
- [Adding a Tool](tools.md) shows how to give a Colleague a new Tool.
- [Evals](evals.md) shows how to test a Protocol with a golden set. This also needs no API key.
- [Approvals](approvals.md) shows how approvers decide by web card or email link.
- [Architecture](architecture.md) shows how a Run moves through the code.
- [Deploying](deploy.md) shows how to put Cograil on a server.
