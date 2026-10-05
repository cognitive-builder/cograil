"""GET and POST /approvals/link/{token}: decide a gate from the signed link in an email.

The link (cograil.approval_links) stands for the approver of that one Approval, so these routes
need no sign-in; they answer 404 unless approval emails are configured. A bad or forged link
answers 403 and changes nothing. GET only shows the call and two buttons: a mail scanner that
opens the link decides nothing, and no GET writes anything (issue #282). POST decides, as
the approver, through the same Runner rules as the signed-in route (the Run's own principal
still cannot be the approver) and records `via: email_link` on the AuditEvent. An Approval is
decided once, so a used link answers 409. A link past its expiry, or whose Approval is already
recorded as expired, answers 410 on both; the Run is escalated to the escalation contact by the
sweep (cograil.approval_sweep), or at once by a POST that finds it still pending.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cograil.api.approvals import decision_failures
from cograil.api.deps import ServicesDep
from cograil.api.schemas import DecisionRequest
from cograil.domain import Approval, RunStatus
from cograil.errors import ApprovalLinkInvalid, ApprovalNotFound, RunNotFound, RunNotPaused

router = APIRouter(prefix="/approvals/link", tags=["approvals"])

Exp = Annotated[int, Query(description="Expiry of the link, as in the email.")]
Sig = Annotated[str, Query(description="Signature of the link, as in the email.")]
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # the signature is in the URL
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class LinkDecision(BaseModel):
    decision: str
    run_status: RunStatus


async def _open_approval(
    token: str, exp: int, sig: str, services: ServicesDep, *, expire: bool
) -> Approval:
    """The Approval a valid link names, still pending and unexpired; otherwise an HTTP error.

    An expired link answers 410. With `expire` (a POST) it also escalates the Run now; without
    it (a GET) nothing is written, as the sweep (cograil.approval_sweep) escalates it."""
    mail = services.approval_mail
    if mail is None:
        raise HTTPException(404, "approval links are not enabled")
    try:
        approval = await services.store.get_approval(token)
        mail.links.verify(approval, exp, sig)
    except (ApprovalNotFound, ApprovalLinkInvalid):
        raise HTTPException(403, "this link is not valid") from None
    if approval.decision == "expired":  # the sweep, or an earlier POST, got there first
        raise HTTPException(410, "this link has expired; the request was escalated")
    if approval.decision != "pending":
        raise HTTPException(409, "this approval was already decided; the link works once")
    if mail.links.expired(exp, datetime.now(UTC)):
        if not expire:
            raise HTTPException(410, "this link has expired")
        try:
            await services.expire(token)
        except (RunNotPaused, RunNotFound):
            raise HTTPException(409, "this approval was already decided") from None
        raise HTTPException(410, "this link has expired; the request was escalated")
    return approval


@router.get("/{token}", response_class=HTMLResponse)
async def show_link(token: str, exp: Exp, sig: Sig, services: ServicesDep) -> HTMLResponse:
    """The call the gate holds and Approve and Decline buttons; decides nothing itself."""
    approval = await _open_approval(token, exp, sig, services, expire=False)
    run = await services.store.get_run(approval.run_id)
    page = _PAGE.format(
        who=html.escape(run.principal_id),
        tool=html.escape(approval.tool),
        step=approval.step,
        args=html.escape(json.dumps(approval.args, indent=2, sort_keys=True, default=str)),
    )
    return HTMLResponse(page, headers=HEADERS)


@router.post("/{token}")
async def decide_link(
    token: str, exp: Exp, sig: Sig, body: DecisionRequest, services: ServicesDep
) -> LinkDecision:
    """Approve or decline as the approver the link stands for."""
    await _open_approval(token, exp, sig, services, expire=True)
    async with decision_failures(refusal="you may not decide this approval"):
        outcome = await services.decide_by_link(token, body.decision)
    return LinkDecision(decision=body.decision, run_status=outcome.run.status)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approval needed</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 36rem; margin: 2rem auto; padding: 0 1rem;
  color-scheme: light dark; }}
pre {{ overflow-x: auto; padding: .75rem; border: 1px solid GrayText; }}
button {{ min-height: 44px; padding: 0 1.25rem; font-size: 1rem; margin-right: .5rem; }}
</style></head><body>
<h1>Approval needed</h1>
<p>{who} asked for step {step} to call <strong>{tool}</strong> with:</p>
<pre>{args}</pre>
<p><button id="approved">Approve</button><button id="declined">Decline</button></p>
<p id="result" role="status"></p>
<script>
for (const decision of ["approved", "declined"]) {{
  document.getElementById(decision).onclick = async () => {{
    const out = document.getElementById("result");
    const r = await fetch(location.href, {{method: "POST",
      headers: {{"content-type": "application/json"}}, body: JSON.stringify({{decision}})}});
    out.textContent = r.ok ? "Recorded: " + decision + "." : "Not recorded: " + await r.text();
    if (r.ok) document.querySelectorAll("button").forEach(b => b.disabled = true);
  }};
}}
</script></body></html>
"""
