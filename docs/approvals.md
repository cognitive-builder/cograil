# Approving by Email or in Chat

A write Tool with `confirm_before_write` stops the Run at a Gate until its approver decides. There are two ways to decide: the approval card in the web chat (see `docs/api.md`) and a signed link in an email.

## Email Links

Set `COGRAIL_EMAIL` to `smtp` or `resend` and the service emails the approver each time a Run stops at a Gate. `.env.example` lists every setting. Settings come only from environment variables. A mode with a missing setting stops the service at start-up with `EmailNotConfigured`. With `COGRAIL_EMAIL` unset, no email is sent and the link routes answer 404.

| Setting | Meaning |
| --- | --- |
| `COGRAIL_EMAIL` | `smtp` or `resend`. |
| `COGRAIL_EMAIL_FROM` | The sender address. |
| `COGRAIL_PUBLIC_URL` | Where the service is reached. Links start with it. |
| `COGRAIL_APPROVAL_LINK_SECRET` | The signing key, 32 characters or more. |
| `COGRAIL_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_STARTTLS` | SMTP. The port is 587 and STARTTLS is on unless you change them. |
| `RESEND_API_KEY` | Resend. |

The email names who asked, the Tool and its arguments, and holds one link: `/approvals/link/{token}?exp=…&sig=…`.

- **The link stands for the approver.** The signature covers the token, the approver and the expiry, so no other Approval, approver or expiry verifies. A forged link answers 403 and changes nothing.
- **Opening it decides nothing.** The page shows the call with Approve and Decline buttons, so a mail scanner that follows the link cannot approve. The buttons POST the decision. The one thing a GET can do is escalate a link that has already expired.
- **It works once.** An Approval is decided once. After that the link answers 409.
- **It expires with the Approval.** The expiry is `approvals.timeout_hours` in `harness.yaml` (72 by default). A link used after that escalates the Run to the Colleague's escalation contact and answers 410.
- **The Runner's rules still apply.** The Run's own principal can never be the approver, and an approval of a Run started under another harness or tool pack is refused.

## Who Is Told

Whenever a Run stops at a Gate, whatever started it (the web chat, Slack or a Schedule), the service tells the approver on every channel that can reach them: the email above when mail is configured, and a Slack direct message when Slack is on and the approver has a `slack_id` (see `docs/slack.md`). Each Approval is sent once per channel.

If no channel can reach the approver, the Run records `approval.undeliverable`. A Run with nobody watching it, which is a scheduled Run, is then escalated to the Colleague's escalation contact at once instead of waiting out the expiry: the Approval is recorded as expired and `run.escalated` carries `reason: approval_undeliverable`. A chat Run is not escalated, because its requester sees the pending Approval and can pass it on.

## What Is Audited

| AuditEvent | When | Detail |
| --- | --- | --- |
| `approval.emailed` | The email was sent. | `approver`, `token`, `step`, `tool`, `expires_at`. Never the link. |
| `approval.email_failed` | The provider refused it. The Run is not failed, and the approver can still use the web card. | The same, plus `error`. |
| `approval.slack_sent` | The Slack direct message was sent. | `approver`, `token`, `step`, `tool`. |
| `approval.slack_failed` | Slack refused it (a missing `im:write` scope, a deactivated member). | The same, plus `error`, the error's type. |
| `approval.undeliverable` | No channel reached the approver. The principal is the Run's. | `approver`, `token`, `step`, `tool`, `tried`, the channels that were offered the Approval. |
| `gate.resumed` or `run.escalated` | The decision. | `decided_by` is the approver and `via` is `email_link`. A decision in the web chat has no `via`. |
