"""GET and POST /approvals/{token}: look at a gate and decide it.

Only the Approval's approver may do either. The decider is the signed-in principal, passed to
`Runner.resume`; the body carries the decision and nothing else, and a body with any other field
is refused. The Runner refuses a decider who is not the approver, or who is the Run's own
principal, and writes a `gate.refused` AuditEvent naming them.

A POST goes on to run the rest of the Run, and answers when the Run next stops.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cograil.api.auth import current_principal
from cograil.api.deps import CurrentPrincipal, ServicesDep
from cograil.api.schemas import ApprovalView, DecisionRequest, RunOutcome
from cograil.api.sse import error_body
from cograil.errors import (
    ApprovalAlreadyDecided,
    ApprovalNotAllowed,
    ApprovalNotFound,
    CograilError,
    RunClaimLost,
    RunNotFound,
    RunNotPaused,
    WorkspaceError,
)

_SHOWN = {"token", "decision", "approver", "step", "tool", "args", "expires_at", "decided_at"}

router = APIRouter(
    prefix="/approvals", tags=["approvals"], dependencies=[Depends(current_principal)]
)


@router.get("/{token}")
async def get_approval(
    token: str, principal: CurrentPrincipal, services: ServicesDep
) -> ApprovalView:
    """The call a gate holds, the Run it belongs to and who asked; approver only."""
    try:
        approval = await services.store.get_approval(token)
        run = await services.store.get_run(approval.run_id)
    except (ApprovalNotFound, RunNotFound):
        raise HTTPException(404, "no such approval") from None
    if approval.approver != principal.id:
        raise HTTPException(403, "you are not the approver of this approval")
    protocol = services.protocol(run.protocol)
    step = (
        next((s for s in protocol.steps if s.number == approval.step), None) if protocol else None
    )
    return ApprovalView(
        **approval.model_dump(include=_SHOWN),
        requested_by=run.principal_id,
        run_id=run.id,
        colleague=run.colleague,
        protocol=run.protocol,
        step_name=step.name if step else None,
    )


@router.post("/{token}")
async def decide_approval(
    token: str, body: DecisionRequest, principal: CurrentPrincipal, services: ServicesDep
) -> RunOutcome:
    """Approve or decline; approved, the Run goes on at the paused Step."""
    try:
        return await services.decide(token, principal, body.decision)
    except (ApprovalNotFound, RunNotFound):
        raise HTTPException(404, "no such approval") from None
    except ApprovalNotAllowed as exc:
        raise HTTPException(403, str(exc)) from None
    except (RunNotPaused, ApprovalAlreadyDecided, RunClaimLost, WorkspaceError) as exc:
        raise HTTPException(409, str(exc)) from None
    except CograilError as exc:
        failure = error_body(exc)
        raise HTTPException(
            500, f"the run failed: {failure['type']}: {failure['message']}"
        ) from None
