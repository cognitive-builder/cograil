"""End to end for issue #10: the example leave_request Protocol runs to completion.

The real example workspace, its parsed Protocol, the mock HRIS and notify packs and the
runner, with FakeProvider in place of the model. One Tool has no kind built yet, so it stands
in: decide.approval_routing (decision tables). The approved Approval stands in for
`cograil approve` (issue #13).
"""

from pathlib import Path
from typing import Any

import pytest

from cograil.domain import Approval, RunStatus
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.registry import build_registry
from cograil.runner import Runner
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

pytestmark = pytest.mark.e2e

EXAMPLE = Path(__file__).parents[1] / "workspaces/example-smb"
STAND_INS = {"decide.approval_routing"}
LEAVE = {
    "employee": "alice",
    "start": "2026-11-02",
    "end": "2026-11-04",
    "leave_type": "annual",
    "request_id": "req-e2e-1",
}
ROUTING = {"duration_days": 3, "leave_type": "annual", "requester_role": "employee"}
NOTE = {"to": "alice@example.com", "message": "Request req-e2e-1 is with bob."}


def call(tool: str, args: dict[str, Any]) -> PlannedToolCall:
    return PlannedToolCall(id=f"call-{tool}", tool=tool, args=args)


SCRIPT = [
    scripted("", call("hris.get_balance", {"employee": "alice"})),
    scripted("You have 25 annual and 10 sick days.", done=True),
    scripted("Confirmed: annual leave from 2026-11-02 to 2026-11-04.", done=True),
    scripted("", call("decide.approval_routing", ROUTING), call("hris.submit_leave", LEAVE)),
    scripted("Submitted; your manager bob must approve.", done=True),
    scripted("", call("notify.send", NOTE)),
    scripted("Done.", done=True),
]


async def stand_in(args: dict[str, Any]) -> dict[str, Any]:
    return {"approver_tier": "manager", "requires_hr": False, "rule": "default"}


async def test_leave_request_runs_to_completion(store: InMemoryRunStore) -> None:
    workspace = load_workspace(EXAMPLE)
    protocol = next(p for p in workspace.protocols if p.name == "leave_request")
    built = [t for t in workspace.tools if t.kind == "python" and t.name not in STAND_INS]
    await store.create_approval(
        Approval(token="a1", run_id="r1", step=3, tool="hris.submit_leave", args=LEAVE,
                 approver="bob", decision="approved")
    )  # fmt: skip
    async with await build_registry(
        workspace.model_copy(update={"tools": built}), store, EXAMPLE
    ) as registry:
        for tool in workspace.tools:
            if tool.name in STAND_INS:
                registry.register(tool, stand_in)
        harper = workspace.colleagues[0]
        run = await Runner(FakeProvider(SCRIPT), registry, store, harper).run("r1", protocol)

    assert (run.status, run.cursor) == (RunStatus.completed, 4)
    assert sorted(run.context["steps"]) == ["1", "2", "3", "4"]
    submitted = run.context["steps"]["3"]["tool_calls"][1]["result"]
    assert (submitted["status"], submitted["remaining"]) == ("submitted", 22)
    calls = await store.list_tool_calls("r1")
    assert [(c.step, c.tool, c.error) for c in calls] == [
        (1, "hris.get_balance", None),
        (3, "decide.approval_routing", None),
        (3, "hris.submit_leave", None),
        (4, "notify.send", None),
    ]
    events = await store.list_audit_events("r1")
    assert (events[0].kind, events[-1].kind) == ("run.started", "run.completed")
    assert {e.principal_id for e in events} == {"alice@example.com"}
