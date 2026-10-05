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
Run), and give it the `chat:write` and `im:write` scopes (`im:write` lets it open a direct
message with an approver). The route has no sign-in of its own: Slack's signature
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
- A message in the thread of a Run continues it. If the Run waits at a gate the reply says whom
  it waits for; if it is still working, or over, the reply says so. A thread starts a second Run
  only if its Run is no longer among the requester's 50 newest.
- A gate's prompt goes to its **approver**, not to the requester's thread. For every Run that
  stops at a gate, whether it started in Slack, in the web chat or on a Schedule, the bot opens a
  direct message with the approver (the approver's `slack_id` in `principals.yaml`, the same map
  reversed) and posts the prompt there: who asked, the Tool, the Step, the call's arguments as
  the approval page shows them, and **Approve** and **Decline** buttons. Arguments longer than
  1000 characters after escaping are clipped, with a pointer to the page for the rest, and then
  the prompt has a **Decline** button only: a clipped call is approved on the approval page or by
  the email link, where it is shown whole (`docs/threat-model.md`). The Run's thread gets a line,
  "Waiting for <approver> to approve.", and no buttons. A message in the thread asking again
  repeats that line; it does not send the approver the prompt a second time.
- An approver with no `slack_id` (or one Slack will not deliver the prompt to, for example
  because the app lacks `im:write`) is sent nothing in Slack, and the thread says which of the
  two it was and points to the link in their email and to the approval page.
- A click is a decision by the Principal the clicking member maps to. The Runner refuses anyone
  but the approver, including the Run's own requester, and writes a `gate.refused` AuditEvent; the
  click is recorded on `gate.resumed` as `via: slack`. The refusal is shown only to the person who
  clicked.
- After a click, the prompt is replaced by what the Runner recorded, not by the button: approved,
  declined, or **expired** (an Approval past its deadline is recorded as an expiry and the Run
  escalates, although the button said Approve). The Run's own outcome follows in its thread. If
  the Run fails after an accepted decision, the thread says so; it is not shown to the approver
  as a refusal.
- Model output is escaped before it is posted, so it cannot ping `@channel` or build a formatted
  link, and Slack is told not to unfurl links, so nothing in it is fetched on its own. A bare URL
  is still shown as a link a person may click.

The full call, and the email link that decides it, are described in [approvals](approvals.md).

## Known limits

This first version has limits you should know before turning it on. They are tracked and will
change:

- **A reply in a channel is visible to everyone in that channel**, entitled or not. Audiences
  and groups decide who may start a Run and what it may read, not who can read the channel. Use
  direct messages for Colleagues that handle protected information (#245).
- **Two members can end up with a Run each in one thread** (the lookup is per requester), and a
  message in a thread repeats the waiting line or reports status; its own text is not used
  (#245).
- **A direct message that also mentions the bot is ignored**, and the Slack path has no message
  length cap like the web chat's (#243).
