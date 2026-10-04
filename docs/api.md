# The Web API

The web service lets a signed-in person chat with a Colleague, follow a Run, and decide an Approval. It is a FastAPI app. The chat answers as server-sent events (SSE), a one-way stream over plain HTTP. Sign-in is described in `docs/auth.md`.

## Starting the Service

```bash
uvicorn --factory cograil.api.wiring:app_from_env
```

Settings come only from environment variables. To run the service in a container on Cloud Run, see `docs/deploy.md`.

| Variable | Meaning |
| --- | --- |
| `COGRAIL_WORKSPACE` | The Workspace folder. The default is the current directory. |
| `DATABASE_URL` | Required. The Postgres store, as a SQLAlchemy URL (`postgresql+asyncpg://...`). In production it names the `cograil_app` role; migrations use `MIGRATIONS_DATABASE_URL` instead. See "Database Roles" in `docs/deploy.md`. |
| `ANTHROPIC_API_KEY` | Read by the Anthropic provider. |
| `COGRAIL_AUTH` | Required. `dev` or `oidc`, with the settings of that mode. See `docs/auth.md`. |

## Endpoints

"Signed in" means any Principal with a valid session. Every endpoint needs one, and answers 401 without it, except `/`, `/history`, `/health`, `/auth/*`, `/docs` and `/openapi.json`.

| Endpoint | What it does | Who may call it |
| --- | --- | --- |
| `GET /` | The web chat page (see below). | Anyone; the page asks `/auth/me` who is signed in. |
| `GET /history` | The run history page (see below). | Anyone; the page asks `/auth/me` who is signed in. |
| `GET /health` | Says the service is up. | Anyone. |
| `/auth/*` | Sign-in routes. | Anyone (see `docs/auth.md`). |
| `POST /chat` | Routes a message to a Protocol and streams the Run as SSE. | Signed in. |
| `GET /runs` | Lists your Runs, newest first, each with the `principal_id` that started it, its `status`, `protocol`, `cost_usd` and `usage` (tokens by category: `input_tokens` and `output_tokens` that were fresh, `cache_read_tokens`, `cache_write_tokens`, and `batch_tokens`); an auditor's list is every Run. `limit` is 1 to 100, default 20. | Signed in. |
| `GET /runs/{id}` | One Run with its Steps, tool calls and Gates. `protocol_changed` is true when the Workspace no longer has the Protocol at the version the Run began with; then `steps` is empty and the rest of the Run is still shown. | The Principal who started it. An auditor lists every Run but does not open another's. |
| `GET /approvals/{token}` | Shows the call a Gate holds, and who asked. | The Approval's approver. |
| `POST /approvals/{token}` | Approves or declines. Body: `{"decision": "approved"}` or `"declined"`. | The Approval's approver. |
| `GET /audit` | Lists AuditEvents, oldest first. | Signed in. You read the events of your own Runs; an auditor reads every Run's. |
| `/docs`, `/openapi.json` | Interactive docs and the OpenAPI document. | Anyone. |

## The Chat Stream

`POST /chat` takes `{"message": "..."}` (1 to 8000 characters) and answers with `text/event-stream`. Each event has a name and a JSON `data` line.

| Event | Meaning |
| --- | --- |
| `routed` | The Colleague and Protocol chosen, with a confidence. Both are null when none fits. |
| `refusal` | Nothing fits that you may start. `text` says what you can ask. The stream ends. |
| `run` | The Run exists. `run_id` names it for `/runs/{id}`. |
| `progress` | One `line` for each AuditEvent and each finished Step, as it is written. |
| `done` | The Run stopped: completed, awaiting an Approval, or escalated. The body carries the Run's `output` (its final answer, once completed) and lists any pending Approvals. The stream ends. |
| `error` | The request failed. `type` is the error class and `message` says why. The stream ends. |

```text
event: routed
data: {"colleague": "helper", "protocol": "record_item", "confidence": 0.93}

event: run
data: {"run_id": "3f9c1e0a5b7d4c2e9a1b6d8f0c2e4a61"}

event: progress
data: {"line": "step 1 complete Look up"}

event: done
data: {"run": {"id": "3f9c...", "status": "awaiting_approval", ...}, "output": null, "awaiting": [{"token": "...", "approver": "manager@example.com", "tool": "demo.record", "step": 2}]}
```

## The Web Chat

Open the service's root (`/`) in a browser. The chat is one HTML file with its own CSS and JavaScript (ADR 0006): no framework, no build step, no request to any other site. The source is `src/cograil/channels/web/chat.html`.

- **Chat.** You type a message and the page sends it to `POST /chat`. The reply streams in: the Colleague and Protocol it was routed to, then one line per `progress` event.
- **Tool calls.** Each tool call shows as a collapsed row (`<details>`). Tap it to see the call's arguments and result, which the page reads from `GET /runs/{id}` once the Run stops.
- **Approval cards.** When a Run stops at a Gate, the page shows a card. If you are the approver, it shows the call and two buttons, Approve and Decline, which `POST /approvals/{token}` with the decision and nothing else. If you are not, it says who the Run waits for. Send the approver a link to `/?approval=<token>` to open the card directly; once a decided Run completes, the card shows the Run's final output.
- **Sign-in.** In `oidc` mode the page shows a Sign in link (to `/auth/login`) until you have a session. Sign-in returns the browser to the page it left, so an approval link keeps working.
- **Phones.** The layout fits a narrow screen, the buttons are at least 44 pixels tall, and the text field is 16 pixels so iOS does not zoom into it. It follows the light or dark setting of the device.

Limits: a decision answers when the Run next stops, not as a stream, so the card shows "going on…" until then. If the connection drops, the Run still carries on; the page says so and names the Run.

## The Run History Page

Open `/history` in a browser (the chat page's header links to it). It is one HTML file the same way the chat is (ADR 0006): no framework, no build step, no request to any other site. The source is `src/cograil/channels/web/history.html`.

- **The list.** The page reads `GET /runs` and shows one row per Run: its status, Colleague and Protocol, the Principal who started it, its cost and tokens so far, and when it began. Your own Runs, or — for a Principal in the workspace's `auditors` group — everyone's.
- **The drill-down.** Opening a row reads `GET /runs/{id}` and shows the Run's Steps with their state and output, every tool call with its arguments and result, its Gates with their decision, approver and expiry, and the timings: when the Run started and stopped and how long it took, and how long each call took. When the Workspace has moved on and the Run's Steps can no longer be shown, the page says so instead of showing a list.
- **Read-only.** The page only ever makes GET requests. Nothing on it can start, decide or change a Run; Refresh reads the list again.

## Rules

- A Run is visible only to its Principal. Another person's Run answers 404, the same as one that does not exist.
- Only the Approval's approver can view or decide it. Anyone else gets 403.
- The decider is always the signed-in Principal. The POST body carries only `decision`. Any other field gives 422, so a client cannot name someone else.
- An approver can also decide from the signed link in an email, with no sign-in (`GET` and `POST /approvals/link/{token}`; see `docs/approvals.md`). The link works once and expires with the Approval.
- The Runner refuses a decider who is not the approver, or who started the Run. It writes a `gate.refused` AuditEvent naming whoever tried.
- A POST to an Approval runs the rest of the Run before it answers. The answer is the Run's state when it next stops; its `awaiting` lists only gates that wait on you. A Run's own chat shows every pending gate to the Principal who started it.
- A missing `DATABASE_URL` stops the service at startup with `StoreNotConfigured`; sign-in problems are the only `AuthNotConfigured` ones.
- Closing the connection does not stop a Run. The stream ends, and the Run carries on to a stop.
- When a message matches, the classification is an AuditEvent `orchestrator.classified` on the new Run. A refusal has no Run, so it is only logged.

## Reading the Audit Log

`GET /audit` returns the AuditEvents of Runs you started, and nothing else. The one exception is an auditor: a Principal that the Workspace's `principals.yaml` puts in the `auditors` group reads the events of every Run. A group that the identity provider claims does not make an auditor, only that file does.

| Query | Meaning |
| --- | --- |
| `limit` | Page size, 1 to 200. The default is 50. |
| `offset` | How many events to skip. The default is 0. |
| `run_id` | Only this Run's events. |
| `principal` | Only events this Principal acted in. On a refused decision, that is the person who tried to decide. |

The answer has `items`, `limit`, `offset` and `next_offset`. Pass `next_offset` as `offset` to get the next page. It is null on the last page.

## Status Codes

| Code | When |
| --- | --- |
| 401 | Nobody is signed in. |
| 403 | You are not the approver of this Approval, or the Runner refused your decision. |
| 404 | No such Run or Approval, or the Run is not yours. |
| 409 | The Approval is already decided, the Run is not paused, or the Workspace changed since the Run began. |
| 422 | The body or a query value is invalid. An extra field in a body counts. |
| 500 | The Run failed while resuming. The body names the error class. |
