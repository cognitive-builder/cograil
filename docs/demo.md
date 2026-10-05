# The Three-Minute Demo

A shot-by-shot script for the launch recording. It uses the `example-smb` Workspace and one story: Alice asks the Colleague Harper for three days of annual leave, the write stops at a Gate, an approver decides from an email, and the audit trail shows everything.

Total running time: **2:55**. Every command is copied from [Quickstart](quickstart.md) and works offline except the chat scene, which uses a real model.

| # | Scene | Time | Length |
| --- | --- | --- | --- |
| 1 | The runbook is Markdown | 0:00–0:20 | 20 s |
| 2 | Validate and compile | 0:20–0:40 | 20 s |
| 3 | Decisions are tables | 0:40–1:00 | 20 s |
| 4 | The Run, live | 1:00–1:45 | 45 s |
| 5 | The Gate and the email | 1:45–2:20 | 35 s |
| 6 | The audit trail | 2:20–2:45 | 25 s |
| 7 | Close | 2:45–2:55 | 10 s |

## Before You Record

Everything in this section happens off camera.

1. Install and start Postgres:

   ```bash
   uv sync
   docker compose up -d postgres
   export DATABASE_URL=postgresql+asyncpg://cograil:cograil@localhost:5432/cograil
   uv run alembic upgrade head
   ```

2. Start a local mail catcher, so approval emails have somewhere to land. Mailpit
   ([MIT licence](https://github.com/axllent/mailpit/blob/main/LICENSE)) is one `docker run`:

   ```bash
   docker run --rm -d --name mailpit -p 1025:1025 -p 8025:8025 axllent/mailpit
   ```

   Its inbox is at `http://localhost:8025`; its SMTP listener is `localhost:1025`.

3. Start the service as Alice, with approval email turned on, pointing at Mailpit:

   ```bash
   export ANTHROPIC_API_KEY=your-key
   export COGRAIL_WORKSPACE=workspaces/example-smb
   export COGRAIL_AUTH=dev COGRAIL_DEV_PRINCIPAL=alice@example.com
   export COGRAIL_EMAIL=smtp
   export COGRAIL_SMTP_HOST=localhost COGRAIL_SMTP_PORT=1025 COGRAIL_SMTP_STARTTLS=false
   export COGRAIL_EMAIL_FROM=cograil@example.com
   export COGRAIL_PUBLIC_URL=http://localhost:8000
   export COGRAIL_APPROVAL_LINK_SECRET=any-string-of-32-characters-or-more
   uv run uvicorn --factory cograil.api.wiring:app_from_env --port 8000
   ```

   Dev mode signs every request in as one Principal (`docs/auth.md`). It is for local work and
   demos only; the recording never shows the startup warning.

4. Open three browser tabs, in this order:

   | Tab | At | Shows |
   | --- | --- | --- |
   | 1 | `http://localhost:8000` | The chat, signed in as `alice@example.com`. |
   | 2 | `http://localhost:8025` | Mailpit, empty, addressed to `hr-ops@example.com`. |
   | 3 | A Mermaid viewer | The output of `cograil graph` below, already pasted. |

5. Record the Mermaid tab's content in advance:

   ```bash
   uv run cograil graph workspaces/example-smb --protocol leave_request
   ```

6. Do one full pass of scenes 4 and 5 before recording. It leaves a finished Run in `/history`
   as backup footage, and it tells you the model's exact wording.

## The Scenes

### 1. The runbook is Markdown — 0:00–0:20

**Show:** `workspaces/example-smb/protocols/leave_request.md` in an editor, scrolling to the
write step.

**Say:** "Cograil turns a Markdown runbook into an AI colleague that follows it step by step.
This is Harper's leave protocol: four Steps, each naming the only Tools it may use. The write
Tool sits behind a Gate."

### 2. Validate and compile — 0:20–0:40

**Show:** A terminal. Run:

```bash
uv run cograil validate workspaces/example-smb
```

One `ok:` line. Then switch to the prepared Mermaid tab: one node per Step, a Gate node before
the write.

**Say:** "The Workspace validates as a whole, and the Protocol compiles to this graph. The
model reasons inside a Step. It never invents steps, tools or gates."

### 3. Decisions are tables — 0:40–1:00

**Show:** The terminal again:

```bash
uv run cograil decide approval_routing --workspace workspaces/example-smb \
  --input duration_days=12 --input leave_type=annual --input requester_role=staff
```

The output names rule `r2`.

**Say:** "Who approves is a decision table, not a prompt. The model supplies the inputs, the
table decides, and the rule id lands in the audit trail. Twelve days is more than ten, so rule
r2 fires: skip-level approval, above Alice's own manager."

### 4. The Run, live — 1:00–1:45

**Show:** Tab 1, the chat. Type exactly:

```text
Annual leave from 2026-11-02 to 2026-11-04, please
```

Let the camera stay on the streaming page: `Routed to harper · leave_request`, each Step
finishing, the `hris.get_balance` call with Alice's 25 days, then the Run stops and a card
appears: *Waiting for hr-ops@example.com to approve. Only they can decide.*

**Say:** "Alice asks in the chat. Harper checks her balance in the HR system, confirms the
dates, and stops at the Gate before submitting anything. Alice cannot approve her own request."

### 5. The Gate and the email — 1:45–2:20

**Show:** Tab 2, Mailpit. A new email to `hr-ops@example.com`: the exact Tool, its arguments,
and one link. Open the link: a page with the call and two buttons. Press **Approve**. The Run
continues and completes on the same page.

**Say:** "The approver gets one signed link: it stands for them, it works once, and opening it
decides nothing. One click, and the Run resumes exactly where it stopped."

### 6. The audit trail — 2:20–2:45

**Show:** Tab 1. Refresh, then open `/history` and expand the finished Run: every Step, every
Tool call with its arguments and result, the Gate, the cost and the token count.

**Say:** "Back in Alice's history: every call, gate and decision is an AuditEvent with the
principal behind it, down to the cent."

### 7. Close — 2:45–2:55

**Show:** The graph from scene 2, or the repository front page.

**Say:** "A runbook in, a colleague out: whitelisted Tools, a Gate on every write, and a trail
the whole way. Cograil is open source — the Workspace in this demo ships in the repo."

## If a Take Goes Wrong

- **A chat turn misbehaves.** The model is real in scene 4, and each message starts a fresh Run
  of the whole Protocol. If Harper ends the Run by asking Alice to confirm the dates instead of
  reaching the Gate, start the take over with a message that states the dates and the leave
  type in one line, as the script's message does.
- **The link is spent or expired.** An Approval is decided once, so start a new Run rather than
  reusing an email. Clear Mailpit between takes so only one approval email is in the inbox.
- **Timing slips.** Scenes 1–3 are fixed output; pause recording between scenes rather than
  rushing scene 4, the only scene that waits on the model.
- **The dry-run Run in `/history`** is your cutaway footage if the live Run needs editing.
