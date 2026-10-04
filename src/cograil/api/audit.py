"""GET /audit: the AuditEvents of the signed-in principal's Runs, a page at a time.

The log is the principal's own: events of Runs they started, oldest first. A principal in the
workspace's `auditors` group (principals.yaml, issue #138) reads every Run's events instead.
`run_id` narrows it to one Run and `principal` to the events a given principal acted in: the
Run's principal on most, and on a refused decision the principal that tried to decide.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from cograil.api.auth import current_principal
from cograil.api.deps import CurrentPrincipal, ServicesDep
from cograil.api.schemas import AuditPage
from cograil.audience import is_auditor
from cograil.identity import normalise_principal_id

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(current_principal)])

MAX_PAGE = 200


@router.get("")
async def list_audit(
    principal: CurrentPrincipal,
    services: ServicesDep,
    run_id: Annotated[str | None, Query(description="Only this Run's events.")] = None,
    acting: Annotated[
        str | None, Query(alias="principal", description="Only events this principal acted in.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditPage:
    """A page of AuditEvents; `next_offset` is null on the last page."""
    wanted = normalise_principal_id(acting) if acting else None
    found = await services.store.page_audit_events(
        owner_id=None if is_auditor(services.workspace, principal) else principal.id,
        run_id=run_id,
        principal_id=wanted,
        limit=limit + 1,
        offset=offset,
    )
    more = len(found) > limit
    return AuditPage(
        items=found[:limit],
        limit=limit,
        offset=offset,
        next_offset=offset + limit if more else None,
    )
