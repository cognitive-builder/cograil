"""GET /runs and /runs/{run_id}: the signed-in principal's Runs, and one Run in detail.

A Run is visible to the principal that started it and to nobody else; another principal's Run
answers 404, as one that does not exist.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from cograil.api.auth import current_principal
from cograil.api.deps import CurrentPrincipal, ServicesDep
from cograil.api.schemas import GateView, RunDetail, RunSummary, StepView
from cograil.errors import RunNotFound

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(current_principal)])

MAX_RUNS = 100


@router.get("")
async def list_runs(
    principal: CurrentPrincipal,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 20,
) -> list[RunSummary]:
    """The principal's Runs, newest first."""
    runs = await services.store.list_runs(limit, principal_id=principal.id)
    return [RunSummary.of(run) for run in runs]


@router.get("/{run_id}")
async def get_run(run_id: str, principal: CurrentPrincipal, services: ServicesDep) -> RunDetail:
    """A Run with its Steps, tool calls and gates."""
    try:
        run = await services.store.get_run(run_id)
    except RunNotFound:
        raise HTTPException(404, "no such run") from None
    if run.principal_id != principal.id:
        raise HTTPException(404, "no such run")
    protocol = services.protocol(run.protocol)
    steps = [StepView.of(step, run) for step in protocol.steps] if protocol else []
    return RunDetail(
        **RunSummary.of(run).model_dump(),
        protocol_version=run.protocol_version,
        harness_version=run.harness_version,
        cursor=run.cursor,
        steps=steps,
        calls=await services.store.list_tool_calls(run.id),
        gates=[GateView.of(a) for a in await services.store.list_approvals(run.id)],
    )
