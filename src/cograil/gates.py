"""Gates the runner enforces from Tool metadata, never from prompt text (ADR 0002).

A scope=write Tool with confirm_before_write runs only against an approved Approval for
the same Run, step, Tool and arguments, and each Approval authorises one call. This is
the fail-closed core: pausing a Run to ask for an Approval, resuming it, and escalating
a declined or expired one arrive with issue #11. Until then a missing Approval raises
GateRequired and the Run fails.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from cograil.domain import Tool
from cograil.errors import GateRequired
from cograil.registry import CallContext
from cograil.store import RunStore


def needs_approval(tool: Tool) -> bool:
    return tool.scope == "write" and tool.confirm_before_write


async def require_approval(
    store: RunStore,
    ctx: CallContext,
    tool: Tool,
    args: Mapping[str, Any],
    used: Collection[str],
) -> str | None:
    """The token of an unused approved Approval for this call; None when no gate applies.

    Raises GateRequired when the Tool is gated and no such Approval exists.
    """
    if not needs_approval(tool):
        return None
    for approval in await store.list_approvals(ctx.run_id):
        if (
            approval.decision == "approved"
            and approval.token not in used
            and approval.step == ctx.step
            and approval.tool == tool.name
            and approval.args == dict(args)
        ):
            return approval.token
    raise GateRequired(f"{tool.name}: step {ctx.step} has no approved Approval for these args")
