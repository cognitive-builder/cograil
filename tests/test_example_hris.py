"""Example-workspace HRIS pack: one test per acceptance criterion of issues #9 and #86."""

from pathlib import Path

import pytest

from cograil.domain import Workspace
from cograil.errors import ToolExecutionError
from cograil.registry import CallContext, build_registry
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
HRIS_NAMES = {"hris.get_balance", "hris.get_manager", "hris.submit_leave"}


def hris_workspace() -> Workspace:
    """The example workspace cut down to its hris pack (notify has its own tests)."""
    workspace = load_workspace(EXAMPLE)
    return workspace.model_copy(
        update={"tools": [t for t in workspace.tools if t.name in HRIS_NAMES]}
    )


def request(employee: str, leave_type: str, request_id: str) -> dict[str, str]:
    return {
        "employee": employee,
        "start": "2026-11-02",
        "end": "2026-11-04",
        "leave_type": leave_type,
        "request_id": request_id,
    }


def test_submit_leave_is_a_gated_write() -> None:
    tool = next(t for t in hris_workspace().tools if t.name == "hris.submit_leave")
    assert (tool.scope, tool.confirm_before_write) == ("write", True)


async def test_submit_leave_decreases_balance_and_replays_idempotently(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        args = request("alice", "annual", "req-1")
        before = await registry.invoke("hris.get_balance", {"employee": "alice"}, ctx)
        first = await registry.invoke("hris.submit_leave", args, ctx)
        replay = await registry.invoke("hris.submit_leave", args, ctx)
        after = await registry.invoke("hris.get_balance", {"employee": "alice"}, ctx)
    assert first["days"] == 3
    assert replay == first
    assert after["annual"] == before["annual"] - 3


async def test_submit_leave_keys_idempotency_on_employee_and_request_id(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        alice = await registry.invoke("hris.submit_leave", request("alice", "annual", "req-1"), ctx)
        bob = await registry.invoke("hris.submit_leave", request("bob", "annual", "req-1"), ctx)
        balances = {
            employee: await registry.invoke("hris.get_balance", {"employee": employee}, ctx)
            for employee in ("alice", "bob")
        }
    assert alice["employee"] == "alice" and bob["employee"] == "bob"
    assert alice["remaining"] == 22 and bob["remaining"] == 17
    assert (balances["alice"]["annual"], balances["bob"]["annual"]) == (22, 17)


async def test_pack_is_registered_and_callable(store: InMemoryRunStore, ctx: CallContext) -> None:
    assert {t.name for t in load_workspace(EXAMPLE).tools} >= HRIS_NAMES
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        manager = await registry.invoke("hris.get_manager", {"employee": "alice"}, ctx)
    assert manager == {"employee": "alice", "manager": "bob"}


@pytest.mark.parametrize(
    ("employee", "balance", "manager"),
    [
        ("alice", {"annual": 25, "sick": 10}, "bob"),
        ("bob", {"annual": 20, "sick": 10}, "carol"),
        ("carol", {"annual": 15, "sick": 10}, "hr-ops@example.com"),
    ],
)
async def test_every_employee_has_balances_and_a_manager(
    employee: str,
    balance: dict[str, int],
    manager: str,
    store: InMemoryRunStore,
    ctx: CallContext,
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        assert await registry.invoke("hris.get_balance", {"employee": employee}, ctx) == balance
        result = await registry.invoke("hris.get_manager", {"employee": employee}, ctx)
    assert result["manager"] == manager


async def test_submit_leave_refuses_more_than_the_remaining_balance(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        args = request("bob", "sick", "req-2") | {"end": "2026-11-20"}
        with pytest.raises(ToolExecutionError, match=r"sick.*19 days requested, 10 remaining"):
            await registry.invoke("hris.submit_leave", args, ctx)
        assert (await registry.invoke("hris.get_balance", {"employee": "bob"}, ctx))["sick"] == 10


async def test_submit_leave_refuses_an_end_before_the_start(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        args = request("bob", "sick", "req-4") | {"start": "2026-11-05", "end": "2026-11-04"}
        with pytest.raises(ToolExecutionError, match=r"end 2026-11-04 is before start 2026-11-05"):
            await registry.invoke("hris.submit_leave", args, ctx)
        assert (await registry.invoke("hris.get_balance", {"employee": "bob"}, ctx))["sick"] == 10


async def test_untracked_leave_type_submits_without_touching_balances(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        args = request("carol", "unpaid", "req-3") | {"start": "2026-12-24", "end": "2026-12-24"}
        result = await registry.invoke("hris.submit_leave", args, ctx)
        balance = await registry.invoke("hris.get_balance", {"employee": "carol"}, ctx)
    assert (result["days"], result["remaining"]) == (1, None)
    assert balance == {"annual": 15, "sick": 10}


async def test_unknown_employee_is_a_tool_execution_error(
    store: InMemoryRunStore, ctx: CallContext
) -> None:
    async with await build_registry(hris_workspace(), store, EXAMPLE) as registry:
        with pytest.raises(ToolExecutionError, match="dave"):
            await registry.invoke("hris.get_balance", {"employee": "dave"}, ctx)
        with pytest.raises(ToolExecutionError, match="dave"):
            await registry.invoke("hris.get_manager", {"employee": "dave"}, ctx)
