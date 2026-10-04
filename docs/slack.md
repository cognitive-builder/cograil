# Slack

The Slack channel lets people talk to a Colleague in Slack. It runs in the same service as the
web chat (`cograil.api.wiring:app_from_env`) and goes through the same code: `Services.chat` to
start a Run and `Services.decide`, the code behind `POST /approvals/{token}`, to decide a gate.

## Turn it on

Install the `slack` extra (`uv sync --extra slack`, or `--build-arg EXTRAS=oidc,slack` for the
image) and set two environment variables:

| Variable | Meaning |
| --- | --- |
| `SLACK_BOT_TOKEN` | The bot token (`xoxb-...`). Unset means the Slack channel is off. |
| `SLACK_SIGNING_SECRET` | Verifies that a request comes from Slack. Required with the token. |

In your Slack app, point both the Events API and Interactivity request URLs at
`https://<your service>/slack/events`. Subscribe the bot to `app_mention` and `message.im`
(add `message.channels` and `message.groups` if replies in a channel thread should continue a
Run), and give it the `chat:write` scope. The route has no sign-in of its own: Slack's signature
is checked first, and a request without a valid one gets a 401.

## Who is who

A Slack member is a Principal through `slack_id` in the workspace's `principals.yaml`:

```yaml
principals:
  - id: alice@example.com
    slack_id: U0123ABCD        # Slack: profile, "Copy member ID"
    groups: [all-employees]
```

The groups are the ones in that file, so audiences and entitlement work exactly as on the web.
A `slack_id` may name only one principal. A member nobody lists gets one reply with their member
id and starts nothing.

## What happens

- A direct message to the bot, or an `@mention`, is a message. It is routed to a Colleague and
  Protocol the member may start, as in the web chat, and starts a Run. Everything about that Run
  is posted in the thread of the message: **one thread per Run**.
- A message in the thread of a Run continues it. If the Run waits at a gate the prompt is posted
  again; if it is still working, or over, the reply says so. A thread starts a second Run only if its Run is no longer among the
  requester's 50 newest.
- A gate is posted as a prompt with **Approve** and **Decline** buttons. A click is a decision
  by the Principal the clicking member maps to. The Runner refuses anyone but the approver,
  including the Run's own requester, and writes a `gate.refused` AuditEvent; the click is
  recorded on `gate.resumed` as `via: slack`. The refusal is shown only to the person who clicked.
- Model output is escaped before it is posted, so it cannot ping `@channel` or make a link.

The prompt names the Tool and the Step, not the call's arguments; the approver sees those on the
approval page (see [approvals](approvals.md)).
