"""The web service (issue #17): /chat over SSE, /runs, /approvals, /audit, /health and /docs.

A real FastAPI app over the cli-demo workspace and an InMemoryRunStore; the model is a
FakeProvider. Who is calling comes from the X-User header, which replaces the sign-in resolver
(sign-in has its own tests in test_api_auth.py)."""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from cograil.api.app import create_app
from cograil.api.auth_settings import auth_settings
from cograil.api.limits import DEFAULT_CHAT_RATE, RateLimit
from cograil.api.wiring import app_from_env, open_registry
from cograil.approval_links import ACCESS_LOGGER, LinkQueryFilter
from cograil.audience import resolve_principal
from cograil.cost import RunUsage, charge, run_usage
from cograil.domain import (
    Colleague,
    Harness,
    Price,
    Principal,
    Protocol,
    Run,
    RunStatus,
    Trigger,
)
from cograil.errors import AuthNotConfigured, StoreNotConfigured
from cograil.orchestrator import ROUTE_TOOL
from cograil.providers import FakeProvider, PlannedToolCall, scripted
from cograil.providers.base import Provider, Usage
from cograil.providers.fake import load_script
from cograil.store import InMemoryRunStore, PostgresRunStore, RunStore
from cograil.workspace import load_workspace

DEMO = Path(__file__).parent / "fixtures/workspaces/cli-demo"
TWO_GATES = Path(__file__).parent / "fixtures/workspaces/two-gates"
ALICE, MANAGER, MALLORY = "alice@example.com", "manager@example.com", "mallory@example.com"
BOSS = "boss@example.com"
AUDITOR = "auditor@example.com"
# Step 1 looks a value up; step 2 plans a gated write, so the Run pauses there.
RUN_SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record, args: {item: "42"}}]
"""
REST_SCRIPT = "- {text: Recorded, done: true}\n"
# The two-gate workspace: the first gate pauses the chat, the second pauses the resume.
TWO_GATE_SCRIPT = """
- tool_calls: [{tool: demo.lookup, args: {key: answer}}]
- {text: Found 42, done: true}
- tool_calls: [{tool: demo.record_a, args: {item: "42"}}]
"""
ON_TO_B_SCRIPT = """
- {text: Recorded A, done: true}
- tool_calls: [{tool: demo.record_b, args: {item: "42"}}]
"""
T0 = datetime(2026, 10, 4, tzinfo=UTC)
PRICED = Harness(pricing={"m": Price(input_per_mtok=1.0, output_per_mtok=2.0)})


class Env:
    """The app under test, the providers it will hand out, and the store behind it."""

    def __init__(
        self,
        tmp_path: Path,
        store: RunStore | None = None,
        close: Any = None,
        root: Path = DEMO,
        approval_mail: Any = None,
        chat_rate: RateLimit | None = DEFAULT_CHAT_RATE,
    ) -> None:
        self.tmp_path = tmp_path
        self.store = store or InMemoryRunStore()
        self.run_scripts: list[Provider] = []
        self.classifier = FakeProvider([])
        workspace = load_workspace(root)
        self.app = create_app(
            workspace,
            root,
            self.store,
            auth=auth_settings({"COGRAIL_AUTH": "dev", "COGRAIL_DEV_PRINCIPAL": ALICE}),
            classifier=self.classifier,
            provider_for=self.next_provider,
            open_registry=open_registry,
            close=close,
            approval_mail=approval_mail,
            chat_rate=chat_rate,
        )
        self.app.state.cograil_principal = lambda request: who(workspace, request)
        self.client = TestClient(self.app)

    def next_provider(self, protocol: Protocol, colleague: Colleague) -> Provider:
        # A decline or a refusal never plans, so it needs no script.
        return self.run_scripts.pop(0) if self.run_scripts else FakeProvider([])

    def script(self, text: str) -> FakeProvider:
        file = self.tmp_path / f"script-{len(self.run_scripts)}.yaml"
        file.write_text(text)
        return FakeProvider(load_script(file))

    def route_to(self, choice: str) -> None:
        args = {"choice": choice, "confidence": 0.9, "reason": "test"}
        call = PlannedToolCall(id="t1", tool=ROUTE_TOOL, args=args)
        self.classifier._script.append(scripted("", call))

    def chat(self, as_: str = ALICE, message: str = "record 42") -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        with self.client.stream(
            "POST", "/chat", json={"message": message}, headers={"X-User": as_}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            name = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    events.append((name, json.loads(line.removeprefix("data: "))))
        return events

    def paused_run(self) -> dict[str, Any]:
        """Alice's chat, ended at the gate; the `done` event."""
        self.route_to("helper/record_item")
        self.run_scripts.append(self.script(RUN_SCRIPT))
        return dict(self.chat())["done"]

    def get(self, path: str, as_: str = ALICE, **params: Any) -> Any:
        return self.client.get(path, params=params, headers={"X-User": as_})

    def post(self, path: str, body: dict[str, Any], as_: str = MANAGER) -> Any:
        return self.client.post(path, json=body, headers={"X-User": as_})


def who(workspace: Any, request: Request) -> Principal:
    user = request.headers.get("X-User")
    if user is None:
        raise HTTPException(401, "sign in")
    return resolve_principal(workspace, user)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield Env(tmp_path)


def stored(env: Env, run_id: str) -> Any:
    return asyncio.run(env.store.get_run(run_id))


def kinds(env: Env, run_id: str) -> list[str]:
    return [e.kind for e in asyncio.run(env.store.list_audit_events(run_id))]


def workspace(env: Env) -> Any:
    """The workspace the app serves, live: a test can move it on under a stored Run."""
    return env.app.state.cograil_services.workspace


# /chat


def test_chat_streams_step_events_until_the_gate(env: Env) -> None:
    env.route_to("helper/record_item")
    env.run_scripts.append(env.script(RUN_SCRIPT))
    events = env.chat()
    names = [name for name, _ in events]
    assert names[0] == "routed" and names[1] == "run" and names[-1] == "done"
    lines = [data["line"] for name, data in events if name == "progress"]
    order = ["run.started", "tool.called", "step 1 complete Look up", "gate.paused"]
    positions = [next(i for i, line in enumerate(lines) if want in line) for want in order]
    assert positions == sorted(positions)
    done = events[-1][1]
    assert done["run"]["status"] == "awaiting_approval"
    assert [(a["approver"], a["tool"]) for a in done["awaiting"]] == [(MANAGER, "demo.record")]
    assert events[0][1] == {"colleague": "helper", "protocol": "record_item", "confidence": 0.9}


def test_chat_audits_the_classification_with_the_principal(env: Env) -> None:
    done = env.paused_run()
    (event,) = [
        e
        for e in asyncio.run(env.store.list_audit_events(done["run"]["id"]))
        if e.kind == "orchestrator.classified"
    ]
    assert event.principal_id == ALICE
    assert event.detail["protocol"] == "record_item" and event.detail["confidence"] == 0.9


def test_chat_stores_the_message_and_requester_as_the_run_input(env: Env) -> None:
    done = env.paused_run()
    run = stored(env, done["run"]["id"])
    assert run.context["input"] == {"message": "record 42", "requester": ALICE}


def test_the_routing_call_is_charged_to_the_run_it_starts(env: Env) -> None:
    services = env.app.state.cograil_services
    harness = services.workspace.harness.model_copy(
        update={"pricing": {"fake-model": Price(input_per_mtok=1.0, output_per_mtok=1.0)}}
    )
    services.workspace = services.workspace.model_copy(update={"harness": harness})
    args = {"choice": "helper/record_item", "confidence": 0.9, "reason": "test"}
    route = PlannedToolCall(id="t1", tool=ROUTE_TOOL, args=args)
    env.classifier._script.append(scripted("", route, input_tokens=1000, output_tokens=200))
    env.run_scripts.append(env.script(RUN_SCRIPT))
    run = stored(env, dict(env.chat())["done"]["run"]["id"])
    # The routing call, then the three turns of RUN_SCRIPT at 10 + 5 tokens each.
    assert run_usage(run) == RunUsage(input_tokens=1000 + 30, output_tokens=200 + 15)
    assert run.cost_usd == pytest.approx((1030 + 215) / 1_000_000)


def test_the_message_reaches_no_audit_event_and_no_log_line(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    pii = "record 42 for Jane Roe, SSN 078-05-1120, jane.roe@example.org"
    env.route_to("helper/record_item")
    env.run_scripts.append(env.script(RUN_SCRIPT))
    with caplog.at_level(logging.DEBUG):
        done = dict(env.chat(message=pii))["done"]
    run_id = done["run"]["id"]
    assert stored(env, run_id).context["input"]["message"] == pii
    events = asyncio.run(env.store.list_audit_events(run_id))
    assert events and all("078-05-1120" not in e.model_dump_json() for e in events)
    assert caplog.records and all("078-05-1120" not in r.getMessage() for r in caplog.records)
    assert all("Jane Roe" not in r.getMessage() for r in caplog.records)


def test_chat_refuses_what_fits_no_protocol_and_starts_no_run(env: Env) -> None:
    env.route_to("none")
    events = env.chat()
    assert [name for name, _ in events] == ["routed", "refusal"]
    assert "record_item" in events[1][1]["text"]
    assert asyncio.run(env.store.list_runs()) == []


def test_chat_ends_with_an_error_event_when_the_work_fails(env: Env) -> None:
    events = env.chat()  # the classifier has no plan to give
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["type"] == "ProviderError"


class TakenOverAtTheStartStore(InMemoryRunStore):
    """A racing execution claims the Run before the chat's own execution can (#104), so the
    Run is never this request's to run."""

    async def claim_run(self, run: Run, read: Run) -> None:
        await super().claim_run(run.model_copy(update={"claim": "racing-execution"}), read)
        await super().claim_run(run, read)


def test_chat_tells_the_caller_the_run_was_taken_over(tmp_path: Path) -> None:
    env = Env(tmp_path, store=TakenOverAtTheStartStore())
    env.route_to("helper/record_item")
    events = env.chat()
    run_id = dict(events)["run"]["run_id"]
    assert [name for name, _ in events] == ["routed", "run", "progress", "error"]
    # a caller error: the message is the run's, not "see the Run's audit log"
    assert events[-1][1] == {"type": "RunClaimLost", "message": run_id}


def test_chat_starts_nothing_outside_the_principals_audience(env: Env) -> None:
    env.route_to("helper/record_item")  # the closed list holds no pair for this principal
    events = env.chat(as_="outsider@example.net")
    assert [name for name, _ in events] == ["routed", "refusal"]
    assert events[0][1]["protocol"] is None
    assert asyncio.run(env.store.list_runs()) == []


def test_chat_needs_a_signed_in_principal(env: Env) -> None:
    assert env.client.post("/chat", json={"message": "hi"}).status_code == 401


@pytest.mark.parametrize(
    "body",
    [{}, {"message": ""}, {"message": "hi", "principal": "x"}, {"message": "x" * 8001}],
)
def test_chat_refuses_a_bad_body(env: Env, body: dict[str, Any]) -> None:
    assert env.client.post("/chat", json=body, headers={"X-User": ALICE}).status_code == 422


# /runs


def with_auditor(env: Env) -> None:
    """The auditor is listed in principals.yaml's `auditors` group; MANAGER is merely staff."""
    workspace(env).principals.append(Principal(id=AUDITOR, groups=["auditors"]))


def test_runs_lists_only_the_principals_own(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    mine = env.get("/runs").json()
    assert [(r["id"], r["status"], r["protocol"]) for r in mine] == [
        (run_id, "awaiting_approval", "record_item")
    ]
    assert env.get("/runs", as_=MALLORY).json() == []
    assert env.client.get("/runs").status_code == 401


def test_an_auditor_lists_every_run_but_opens_only_their_own(env: Env) -> None:
    with_auditor(env)
    run_id = env.paused_run()["run"]["id"]
    assert [r["id"] for r in env.get("/runs", as_=AUDITOR).json()] == [run_id]
    assert env.get(f"/runs/{run_id}", as_=AUDITOR).status_code == 404  # detail is the starter's
    for caller in (MANAGER, MALLORY):
        assert env.get("/runs", as_=caller).json() == []
        assert env.get(f"/runs/{run_id}", as_=caller).status_code == 404
    assert env.get("/runs/nope", as_=AUDITOR).status_code == 404


def test_each_listed_run_names_who_started_it(env: Env) -> None:
    # The history page lists runs with status, protocol, principal and cost (#26); an auditor's
    # rows are everyone's, so each one has to name its own principal.
    run_id = env.paused_run()["run"]["id"]
    assert [(r["id"], r["principal_id"]) for r in env.get("/runs").json()] == [(run_id, ALICE)]
    with_auditor(env)
    assert [(r["id"], r["principal_id"]) for r in env.get("/runs", as_=AUDITOR).json()] == [
        (run_id, ALICE)
    ]


def test_each_listed_run_shows_its_tokens_by_category(env: Env) -> None:
    env.paused_run()
    (row,) = env.get("/runs").json()
    assert set(row["usage"]) == {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "batch_tokens",
    }
    assert row["cost_usd"] >= 0


def test_the_list_and_detail_carry_the_runs_exact_usage_and_cost(env: Env) -> None:
    # The values behind the shape the test above checks (issue #229): a Run whose tally is
    # known reports its exact numbers on both endpoints, so a fall to all-zero usage fails.
    run = Run(id="r-values", workspace="cli-demo", colleague="helper", protocol="record_item",
              protocol_version=1, principal=Principal(id=ALICE), trigger=Trigger(kind="chat"),
              status=RunStatus.completed, created_at=T0, updated_at=T0)  # fmt: skip
    run = charge(
        PRICED,
        run,
        "m",
        Usage(
            input_tokens=1_000_000,
            output_tokens=250_000,
            cache_read_tokens=500_000,
            cache_write_tokens=200_000,
        ),
    )
    run = charge(PRICED, run, "m", Usage(input_tokens=100_000))  # summed, not overwritten
    asyncio.run(env.store.create_run(run))
    tally = {
        "input_tokens": 1_100_000,
        "output_tokens": 250_000,
        "cache_read_tokens": 500_000,
        "cache_write_tokens": 200_000,
        "batch_tokens": 0,
    }
    (row,) = env.get("/runs").json()
    assert row["usage"] == tally
    assert row["cost_usd"] == pytest.approx(1.9)  # 1.8 with cache at 0.1x/1.25x, then 0.1 fresh
    detail = env.get(f"/runs/{row['id']}").json()
    assert detail["usage"] == tally and detail["cost_usd"] == pytest.approx(1.9)


def test_run_detail_shows_steps_calls_and_gates(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    detail = env.get(f"/runs/{run_id}").json()
    assert [(s["number"], s["name"], s["state"]) for s in detail["steps"]] == [
        (1, "Look up", "done"),
        (2, "Record", "current"),
    ]
    assert detail["steps"][0]["output"] == "Found 42"
    assert [c["tool"] for c in detail["calls"]] == ["demo.lookup"]
    (gate,) = detail["gates"]
    assert (gate["tool"], gate["approver"], gate["decision"]) == ("demo.record", MANAGER, "pending")
    assert "token" not in gate
    assert detail["protocol_changed"] is False


def test_run_detail_carries_the_timings_the_history_page_shows(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    detail = env.get(f"/runs/{run_id}").json()
    assert detail["created_at"] <= detail["updated_at"]  # ISO 8601, both UTC
    (call,) = detail["calls"]
    assert call["started_at"] <= call["ended_at"]
    assert detail["gates"][0]["expires_at"] is not None  # a pending gate says when it expires


@pytest.mark.parametrize("moved_on", ["new-version", "gone"])
def test_run_detail_says_when_the_workspaces_protocol_moved_on(env: Env, moved_on: str) -> None:
    run_id = env.paused_run()["run"]["id"]
    protocol = next(p for p in workspace(env).protocols if p.name == "record_item")
    if moved_on == "new-version":
        protocol.version += 1
    else:
        workspace(env).protocols.remove(protocol)
    detail = env.get(f"/runs/{run_id}").json()
    assert detail["protocol_changed"] is True
    assert detail["steps"] == []  # the Workspace's Steps are no longer the ones that ran
    assert detail["protocol_version"] == 1 and detail["protocol"] == "record_item"


@pytest.mark.parametrize("caller", [MALLORY, MANAGER])
def test_a_run_is_visible_to_its_principal_only(env: Env, caller: str) -> None:
    run_id = env.paused_run()["run"]["id"]
    assert env.get(f"/runs/{run_id}", as_=caller).status_code == 404
    assert env.get("/runs/nope").status_code == 404


# /approvals


def pending_token(env: Env) -> str:
    return str(env.paused_run()["awaiting"][0]["token"])


def test_approval_get_renders_the_gate_for_its_approver(env: Env) -> None:
    token = pending_token(env)
    view = env.get(f"/approvals/{token}", as_=MANAGER).json()
    assert (view["tool"], view["args"], view["requested_by"]) == (
        "demo.record",
        {"item": "42"},
        ALICE,
    )
    assert (view["protocol"], view["step_name"], view["decision"]) == (
        "record_item",
        "Record",
        "pending",
    )


@pytest.mark.parametrize("caller", [ALICE, MALLORY])
def test_approval_get_is_for_the_approver_only(env: Env, caller: str) -> None:
    token = pending_token(env)
    assert env.get(f"/approvals/{token}", as_=caller).status_code == 403
    assert env.get("/approvals/unknown", as_=MANAGER).status_code == 404


def test_approval_post_approves_and_the_run_goes_on_at_the_paused_step(env: Env) -> None:
    done = env.paused_run()
    run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
    env.run_scripts.append(env.script(REST_SCRIPT))
    response = env.post(f"/approvals/{token}", {"decision": "approved"})
    assert response.status_code == 200, response.text
    assert response.json()["run"]["status"] == "completed"
    run = stored(env, run_id)
    assert (run.status, run.cursor) == (RunStatus.completed, 2)
    resumed = [
        e for e in asyncio.run(env.store.list_audit_events(run_id)) if e.kind == "gate.resumed"
    ]
    assert [e.detail["decided_by"] for e in resumed] == [MANAGER]


def test_an_approval_answers_with_the_runs_final_output(env: Env) -> None:
    token = pending_token(env)
    env.run_scripts.append(env.script(REST_SCRIPT))
    outcome = env.post(f"/approvals/{token}", {"decision": "approved"}).json()
    assert outcome["run"]["status"] == "completed"
    assert outcome["output"] == "Recorded"  # the last Step's answer, not step 1's "Found 42"


def test_a_run_that_has_not_completed_has_no_final_output(env: Env) -> None:
    done = env.paused_run()  # waiting at the gate
    assert done["output"] is None
    declined = env.post(f"/approvals/{done['awaiting'][0]['token']}", {"decision": "declined"})
    assert declined.json()["run"]["status"] == "escalated"
    assert declined.json()["output"] is None


def test_a_decider_is_shown_only_their_own_gates(tmp_path: Path) -> None:
    # Two gates, two approvers: the Run pauses at manager's gate, then at the boss's.
    env = Env(tmp_path, root=TWO_GATES)
    env.route_to("gatekeeper/double_record")
    env.run_scripts.append(env.script(TWO_GATE_SCRIPT))
    done = dict(env.chat())["done"]
    run_id, gate_a = done["run"]["id"], done["awaiting"][0]["token"]
    assert done["awaiting"][0]["approver"] == MANAGER
    # The interim approver rule pins every gate to the Colleague's escalation_contact, so
    # moving it between the two pauses stands in for the approver routing still to land.
    gatekeeper = next(c for c in workspace(env).colleagues if c.name == "gatekeeper")
    gatekeeper.escalation_contact = BOSS
    env.run_scripts.append(env.script(ON_TO_B_SCRIPT))
    answer = env.post(f"/approvals/{gate_a}", {"decision": "approved"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["awaiting"] == []  # the boss's gate is not the manager's to see
    pending = [a for a in asyncio.run(env.store.list_approvals(run_id)) if a.decision == "pending"]
    assert [(a.approver, a.tool, a.step) for a in pending] == [(BOSS, "demo.record_b", 3)]
    env.run_scripts.append(env.script(REST_SCRIPT))
    answer = env.post(f"/approvals/{pending[0].token}", {"decision": "approved"}, as_=BOSS)
    assert answer.json()["run"]["status"] == "completed"


def test_approval_post_declined_escalates(env: Env) -> None:
    done = env.paused_run()
    token = done["awaiting"][0]["token"]
    response = env.post(f"/approvals/{token}", {"decision": "declined"})
    assert response.json()["run"]["status"] == "escalated"
    assert "run.escalated" in kinds(env, done["run"]["id"])


@pytest.mark.parametrize("decider", [ALICE, MALLORY], ids=["the-requester", "someone-else"])
def test_approval_post_refuses_anyone_but_the_approver(env: Env, decider: str) -> None:
    done = env.paused_run()
    run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
    response = env.post(f"/approvals/{token}", {"decision": "approved"}, as_=decider)
    assert response.status_code == 403
    assert stored(env, run_id).status is RunStatus.awaiting_approval
    (refused,) = [
        e for e in asyncio.run(env.store.list_audit_events(run_id)) if e.kind == "gate.refused"
    ]
    assert refused.principal_id == decider


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "approved", "decided_by": MANAGER},
        {"decision": "approved", "decider": MANAGER},
        {"decision": "maybe"},
        {},
    ],
)
def test_approval_post_never_takes_the_decider_from_the_body(
    env: Env, body: dict[str, Any]
) -> None:
    done = env.paused_run()
    run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
    assert env.post(f"/approvals/{token}", body, as_=ALICE).status_code == 422
    assert stored(env, run_id).status is RunStatus.awaiting_approval


def test_approval_post_twice_is_a_conflict(env: Env) -> None:
    token = pending_token(env)
    env.run_scripts.append(env.script(REST_SCRIPT))
    assert env.post(f"/approvals/{token}", {"decision": "approved"}).status_code == 200
    env.run_scripts.append(env.script(REST_SCRIPT))
    assert env.post(f"/approvals/{token}", {"decision": "approved"}).status_code == 409


class TakenOverAfterTheDecisionStore(InMemoryRunStore):
    """A racing execution claims the decided Run before the resume's claim lands (#104): the
    Run the decision set running is no longer this request's to go on with."""

    async def claim_run(self, run: Run, read: Run) -> None:
        if read.status is RunStatus.running:  # the read a resume claims from
            await super().claim_run(run.model_copy(update={"claim": "racing-execution"}), read)
        await super().claim_run(run, read)


def test_approval_post_is_a_conflict_when_the_run_was_taken_over(tmp_path: Path) -> None:
    env = Env(tmp_path, store=TakenOverAfterTheDecisionStore())
    done = env.paused_run()
    run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
    assert env.post(f"/approvals/{token}", {"decision": "approved"}).status_code == 409
    assert stored(env, run_id).status is RunStatus.running  # the racing execution's to finish


def test_approval_post_for_an_unknown_token_is_404(env: Env) -> None:
    assert env.post("/approvals/unknown", {"decision": "approved"}).status_code == 404


# /audit


def test_audit_pages_through_the_principals_events(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    everything = kinds(env, run_id)
    first = env.get("/audit", limit=2).json()
    assert [e["kind"] for e in first["items"]] == everything[:2]
    assert (first["limit"], first["offset"], first["next_offset"]) == (2, 0, 2)
    seen = [e["kind"] for e in first["items"]]
    page = first
    while page["next_offset"] is not None:
        page = env.get("/audit", limit=2, offset=page["next_offset"]).json()
        seen += [e["kind"] for e in page["items"]]
    assert seen == everything


def test_audit_filters_by_run_and_principal(env: Env) -> None:
    done = env.paused_run()
    run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
    assert env.post(f"/approvals/{token}", {"decision": "approved"}, as_=MALLORY).status_code == 403
    env.route_to("helper/record_item")
    env.run_scripts.append(env.script(RUN_SCRIPT))
    other = dict(env.chat())["done"]["run"]["id"]

    in_run = env.get("/audit", run_id=other).json()["items"]
    assert in_run and {e["run_id"] for e in in_run} == {other}
    by_mallory = env.get("/audit", principal=MALLORY.upper()).json()["items"]
    assert [(e["run_id"], e["kind"]) for e in by_mallory] == [(run_id, "gate.refused")]
    assert env.get("/audit", run_id=other, principal=MALLORY).json()["items"] == []


def test_audit_is_the_principals_own_and_needs_sign_in(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    assert env.get("/audit", as_=MALLORY).json()["items"] == []
    assert env.get("/audit", as_=MALLORY, run_id=run_id).json()["items"] == []
    assert env.client.get("/audit").status_code == 401


def test_an_auditor_reads_every_principals_events_and_others_do_not(env: Env) -> None:
    with_auditor(env)
    run_id = env.paused_run()["run"]["id"]
    everything = kinds(env, run_id)
    assert [e["kind"] for e in env.get("/audit", as_=AUDITOR).json()["items"]] == everything
    assert {e["run_id"] for e in env.get("/audit", as_=AUDITOR, run_id=run_id).json()["items"]} == {
        run_id
    }
    for caller in (MANAGER, MALLORY):
        assert env.get("/audit", as_=caller).json()["items"] == []
        assert env.get("/audit", as_=caller, run_id=run_id).json()["items"] == []


def test_the_principal_filter_still_narrows_an_auditors_events(env: Env) -> None:
    with_auditor(env)
    done = env.paused_run()
    token = done["awaiting"][0]["token"]
    assert env.post(f"/approvals/{token}", {"decision": "approved"}, as_=MALLORY).status_code == 403
    by_alice = env.get("/audit", as_=AUDITOR, principal=ALICE).json()["items"]
    by_mallory = env.get("/audit", as_=AUDITOR, principal=MALLORY).json()["items"]
    assert by_alice and {e["principal_id"] for e in by_alice} == {ALICE}
    assert [e["kind"] for e in by_mallory] == ["gate.refused"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_audit_refuses_a_bad_page(env: Env, params: dict[str, int]) -> None:
    assert env.get("/audit", **params).status_code == 422


# /health and /docs


def test_health_and_openapi_docs_are_served(env: Env) -> None:
    assert env.client.get("/health").json() == {"status": "ok"}
    assert env.client.get("/docs").status_code == 200
    paths = env.client.get("/openapi.json").json()["paths"]
    assert {"/chat", "/runs", "/runs/{run_id}", "/approvals/{token}", "/audit", "/health"} <= set(
        paths
    )
    assert set(paths["/approvals/{token}"]) == {"get", "post"}


def test_the_app_keeps_link_signatures_out_of_the_access_log(env: Env) -> None:
    """The filter itself is tested in test_approval_links; here, that the app installs it."""
    access = logging.getLogger(ACCESS_LOGGER)
    assert any(isinstance(f, LinkQueryFilter) for f in access.filters)


# wiring


def test_app_from_env_without_a_database_url_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(StoreNotConfigured, match="DATABASE_URL") as raised:
        app_from_env()
    assert not isinstance(raised.value, AuthNotConfigured)


# End to end (issue labelled needs:e2e): the same flow over Postgres. Everything goes through
# HTTP, so the engine only ever lives in the app's own event loop.


@pytest.mark.e2e
def test_chat_then_approve_through_postgres(migrated_url: str, tmp_path: Path) -> None:
    store = PostgresRunStore.from_url(migrated_url)
    env = Env(tmp_path, store, close=store.dispose)
    with env.client:
        done = env.paused_run()
        run_id, token = done["run"]["id"], done["awaiting"][0]["token"]
        assert (
            env.post(f"/approvals/{token}", {"decision": "approved"}, as_=ALICE).status_code == 403
        )
        env.run_scripts.append(env.script(REST_SCRIPT))
        assert env.post(f"/approvals/{token}", {"decision": "approved"}).json()["run"][
            "status"
        ] == ("completed")
        detail = env.get(f"/runs/{run_id}").json()
        assert [s["state"] for s in detail["steps"]] == ["done", "done"]
        assert [g["decision"] for g in detail["gates"]] == ["approved"]
        events = env.get("/audit", run_id=run_id, limit=200).json()["items"]
        assert [e["kind"] for e in events].count("gate.refused") == 1
        assert "orchestrator.classified" in [e["kind"] for e in events]
