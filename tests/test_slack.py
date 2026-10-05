"""The Slack channel (issue #27): messages start or continue a Run in one thread, approval
buttons decide as the Principal the clicking member maps to, and members map to principals
through principals.yaml. A fake Poster stands in for Slack; no test calls Slack."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slack_sdk.web.async_client import AsyncWebClient
from test_api import DEMO, REST_SCRIPT, RUN_SCRIPT, Env

from cograil.api.app import create_app
from cograil.api.auth_settings import auth_settings
from cograil.api.services import Services
from cograil.api.wiring import open_registry
from cograil.channels.slack import (
    Incoming,
    SlackChannel,
    SlackSettings,
    principal_for_slack_user,
    slack_settings,
)
from cograil.channels.slack.blocks import APPROVE_ACTION, DECLINE_ACTION, MAX_TEXT, args_text
from cograil.channels.slack.bolt import (
    BoltPoster,
    SlackListeners,
    incoming_from_event,
    slack_router,
)
from cograil.channels.slack.identity import escape
from cograil.domain import RunStatus
from cograil.errors import SlackNotConfigured, WorkspaceError
from cograil.workspace import load_workspace

ALICE, MANAGER, OUTSIDER = "U_ALICE", "U_MANAGER", "U_OUTSIDER"
DM = "D1"  # Alice's direct message with the bot, where her Run's thread is
MANAGER_DM = "D2"  # the manager's: Slack gives each member their own conversation
DMS = {ALICE: DM, MANAGER: MANAGER_DM, OUTSIDER: "D3"}
ALICE_ID = "alice@example.com"


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.whispers: list[dict[str, Any]] = []
        self.replaced: list[dict[str, Any]] = []
        self.refuse_dm = False

    async def post(
        self,
        channel: str,
        thread_ts: str | None,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.posts.append(
            {"channel": channel, "thread_ts": thread_ts, "text": text, "blocks": blocks}
        )

    async def open_dm(self, user: str) -> str:
        if self.refuse_dm:
            raise RuntimeError("missing_scope")
        return DMS[user]

    def in_channel(self, channel: str) -> list[dict[str, Any]]:
        return [p for p in self.posts if p["channel"] == channel]

    async def whisper(self, channel: str, thread_ts: str, user: str, text: str) -> None:
        self.whispers.append({"user": user, "text": text})

    async def replace(self, channel: str, ts: str, text: str) -> None:
        self.replaced.append({"ts": ts, "text": text})

    def buttons(self) -> list[dict[str, Any]]:
        return [b for p in self.posts for blk in p["blocks"] or [] for b in blk.get("elements", [])]

    def token(self) -> str:
        return str(self.buttons()[0]["value"])


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """The cli-demo workspace with Slack member ids in its principals.yaml."""
    root = tmp_path / "pack"
    shutil.copytree(DEMO, root)
    people = [
        {"id": "alice@example.com", "slack_id": ALICE, "groups": ["staff"]},
        {"id": "manager@example.com", "slack_id": MANAGER, "groups": ["staff"]},
        {"id": "outsider@example.net", "slack_id": OUTSIDER, "groups": []},
    ]
    (root / "principals.yaml").write_text(yaml.safe_dump({"principals": people}))
    return root


@pytest.fixture
def env(tmp_path: Path, pack: Path) -> Env:
    return Env(tmp_path, root=pack)


@pytest.fixture
def services(env: Env) -> Services:
    services: Services = env.app.state.cograil_services
    return services


@pytest.fixture
def channel(services: Services) -> SlackChannel:
    return SlackChannel(services)


def say(
    text: str = "record 42", *, user: str = ALICE, thread: str = "100.1", dm: bool = True
) -> Incoming:
    return Incoming(user=user, channel=DM, thread_ts=thread, text=text, addressed=dm)


async def start(env: Env, channel: SlackChannel, poster: FakePoster, **kwargs: Any) -> None:
    env.route_to("helper/record_item")
    env.run_scripts.append(env.script(RUN_SCRIPT))
    await channel.on_message(say(**kwargs), poster)


async def decide(
    channel: SlackChannel, poster: FakePoster, *, user: str = MANAGER, decision: Any = "approved"
) -> None:
    """A click on the prompt, in the clicking member's own conversation with the bot."""
    where = DMS.get(user, "D9")
    await channel.on_decision(user, where, "300.1", "300.1", poster.token(), decision, poster)


# Messages to the bot start or continue a run; one thread per run


async def test_a_message_starts_a_run_and_answers_in_its_thread(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    (run,) = await services.store.list_runs()
    assert run.trigger.channel == "slack" and run.principal_id == "alice@example.com"
    assert run.context["slack"] == {"channel": DM, "thread_ts": "100.1"}
    assert run.context["input"]["message"] == "record 42"
    assert run.status is RunStatus.awaiting_approval
    assert {p["thread_ts"] for p in poster.in_channel(DM)} == {"100.1"}
    assert [b["action_id"] for b in poster.buttons()] == [APPROVE_ACTION, DECLINE_ACTION]


async def test_each_run_gets_its_own_thread(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster, thread="100.1")
    await start(env, channel, poster, thread="200.1")
    runs = await services.store.list_runs()
    assert sorted(r.context["slack"]["thread_ts"] for r in runs) == ["100.1", "200.1"]


async def test_a_message_in_a_runs_thread_continues_it_without_starting_another(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    await channel.on_message(say("any news?", dm=False), poster)  # no mention, still the thread
    assert len(await services.store.list_runs()) == 1
    assert len(poster.buttons()) == 2  # the approver is not sent the gate a second time
    assert poster.in_channel(DM)[-1]["text"] == f"Waiting for <@{MANAGER}> to approve."
    assert {p["thread_ts"] for p in poster.in_channel(DM)} == {"100.1"}


async def test_a_finished_runs_thread_says_so_and_starts_nothing(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    env.run_scripts.append(env.script(REST_SCRIPT))
    await decide(channel, poster)
    await channel.on_message(say("thanks"), poster)
    assert "completed" in poster.posts[-1]["text"]
    assert len(await services.store.list_runs()) == 1


async def test_a_plain_channel_message_outside_any_run_is_ignored(
    channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await channel.on_message(say(dm=False), poster)
    assert poster.posts == [] and await services.store.list_runs() == []


# Approval buttons decide through the approvals code


async def test_the_approvers_click_approves_and_the_run_goes_on(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    env.run_scripts.append(env.script(REST_SCRIPT))
    await decide(channel, poster)
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.completed
    assert poster.replaced == [
        {"ts": "300.1", "text": f"Approved by <@{MANAGER}>: `demo.record` (step 2)."}
    ]
    assert (poster.posts[-1]["channel"], poster.posts[-1]["thread_ts"]) == (DM, "100.1")
    assert poster.posts[-1]["text"] == "Recorded"
    resumed = [
        e for e in await services.store.list_audit_events(run.id) if e.kind == "gate.resumed"
    ]
    assert [(e.detail["decided_by"], e.detail["via"]) for e in resumed] == [
        ("manager@example.com", "slack")
    ]


async def test_a_decline_declines(env: Env, channel: SlackChannel, services: Services) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    await decide(channel, poster, decision="declined")
    (run,) = await services.store.list_runs()
    assert run.status is not RunStatus.awaiting_approval
    assert poster.replaced[0]["text"].startswith("Declined")


@pytest.mark.parametrize(("clicker", "told"), [
    (ALICE, "not the approver"),  # the requester cannot approve their own request
    (OUTSIDER, "not the approver"),
    ("U_STRANGER", "don't know your Slack account"),
])  # fmt: skip
async def test_only_the_approver_decides(
    env: Env, channel: SlackChannel, services: Services, clicker: str, told: str
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    await decide(channel, poster, user=clicker)
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.awaiting_approval and poster.replaced == []
    assert told in poster.whispers[0]["text"]
    refused = [
        e for e in await services.store.list_audit_events(run.id) if e.kind == "gate.refused"
    ]
    assert len(refused) == (clicker != "U_STRANGER")


async def test_a_second_click_changes_nothing(env: Env, channel: SlackChannel) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    env.run_scripts.append(env.script(REST_SCRIPT))
    await decide(channel, poster)
    await decide(channel, poster)
    assert "not paused" in poster.whispers[0]["text"]  # the Run went on at the first decision


# The prompt goes to the approver, with what is being approved (#242)


async def test_the_prompt_reaches_the_approver_not_the_requesters_thread(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    assert poster.buttons() and all(p["blocks"] is None for p in poster.in_channel(DM))
    (prompt,) = poster.in_channel(MANAGER_DM)  # their own conversation, top level
    assert prompt["thread_ts"] is None and prompt["blocks"] is not None
    assert poster.in_channel(DM)[-1]["text"] == f"Waiting for <@{MANAGER}> to approve."
    # The click comes from the approver's conversation, a different one from the requester's.
    env.run_scripts.append(env.script(REST_SCRIPT))
    await decide(channel, poster)
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.completed
    assert [r["ts"] for r in poster.replaced] == ["300.1"]
    assert (poster.posts[-1]["channel"], poster.posts[-1]["text"]) == (DM, "Recorded")


async def test_the_requester_cannot_decide_from_their_own_conversation(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    await channel.on_decision(ALICE, DM, "100.1", "100.1", poster.token(), "approved", poster)
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.awaiting_approval
    assert "not the approver" in poster.whispers[0]["text"]


async def test_the_prompt_names_the_requester_the_tool_the_step_and_the_arguments(
    env: Env, channel: SlackChannel
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    text = poster.in_channel(MANAGER_DM)[0]["blocks"][0]["text"]["text"]
    assert f"<@{ALICE}>" in text and "`demo.record`" in text and "step 2" in text
    assert '"item": "42"' in text


@pytest.mark.parametrize(
    ("args", "clipped"),
    [({"item": "42"}, False), ({"item": "x" * 5000}, True)],
)
def test_arguments_are_clipped_with_a_pointer_to_the_approval_page(
    args: dict[str, str], clipped: bool
) -> None:
    text = args_text(args)
    assert ("The rest is on the approval page" in text) == clipped
    assert len(text) < MAX_TEXT


def test_arguments_cannot_ping_or_close_the_code_block() -> None:
    text = args_text({"note": "<!channel> ``` <@U1>"})
    assert "<" not in text and ">" not in text
    assert text.count("```") == 2


async def test_an_approver_without_a_slack_id_is_told_nothing_in_slack(
    tmp_path: Path, pack: Path
) -> None:
    people = [
        {"id": "alice@example.com", "slack_id": ALICE, "groups": ["staff"]},
        {"id": "manager@example.com", "groups": ["staff"]},
    ]
    (pack / "principals.yaml").write_text(yaml.safe_dump({"principals": people}))
    env = Env(tmp_path, root=pack)
    channel = SlackChannel(env.app.state.cograil_services)
    poster = FakePoster()
    await start(env, channel, poster)
    assert poster.buttons() == [] and {p["channel"] for p in poster.posts} == {DM}
    said = poster.posts[-1]["text"]
    assert "manager@example.com" in said and "email" in said and "approval page" in said


async def test_a_prompt_slack_will_not_deliver_is_said_in_the_thread(
    env: Env, channel: SlackChannel
) -> None:
    poster = FakePoster()
    poster.refuse_dm = True
    await start(env, channel, poster)
    assert poster.buttons() == [] and "approval page" in poster.posts[-1]["text"]


# What the thread says is what the Runner recorded


async def test_an_approval_past_its_deadline_is_announced_as_expired(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    token = poster.token()
    stale = env.store._approvals[token]  # type: ignore[attr-defined]
    past = datetime.now(UTC) - timedelta(minutes=1)
    env.store._approvals[token] = stale.model_copy(update={"expires_at": past})  # type: ignore[attr-defined]
    await decide(channel, poster)  # the button said Approve
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.escalated
    (replaced,) = poster.replaced
    assert replaced["text"].startswith("Expired") and "Approved" not in replaced["text"]


async def test_a_run_that_fails_after_an_approval_is_reported_in_its_thread(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await start(env, channel, poster)
    await decide(channel, poster)  # no script for the next Step: it raises, and the Run fails
    (run,) = await services.store.list_runs()
    assert run.status is RunStatus.failed
    assert poster.whispers == []  # not shown to the clicker as a refusal
    assert poster.replaced[0]["text"].startswith("Approved")  # the buttons are gone
    assert (poster.posts[-1]["channel"], poster.posts[-1]["thread_ts"]) == (DM, "100.1")
    assert "failed after the decision" in poster.posts[-1]["text"]


# Slack user mapped to a principal and groups via workspace config


def test_a_member_maps_to_the_principal_and_groups_in_principals_yaml(pack: Path) -> None:
    workspace = load_workspace(pack)
    alice = principal_for_slack_user(workspace, ALICE)
    assert alice is not None and (alice.id, alice.groups) == ("alice@example.com", ["staff"])
    assert principal_for_slack_user(workspace, "U_NOBODY") is None


def test_a_slack_id_may_not_name_two_principals(pack: Path) -> None:
    people = [
        {"id": "a@example.com", "slack_id": ALICE},
        {"id": "b@example.com", "slack_id": ALICE},
    ]
    (pack / "principals.yaml").write_text(yaml.safe_dump({"principals": people}))
    with pytest.raises(WorkspaceError, match=f"{ALICE} names both a@example.com and b@example.com"):
        load_workspace(pack)


async def test_an_unmapped_member_starts_nothing_and_is_told_their_id(
    channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    await channel.on_message(say(user="U_STRANGER"), poster)
    assert "U_STRANGER" in poster.posts[0]["text"] and await services.store.list_runs() == []


async def test_groups_decide_what_a_member_may_start(
    env: Env, channel: SlackChannel, services: Services
) -> None:
    poster = FakePoster()
    env.route_to("helper/record_item")  # the closed list holds no pair for a member with no groups
    await channel.on_message(say(user=OUTSIDER), poster)
    assert await services.store.list_runs() == [] and len(poster.posts) == 1


# Settings and the Bolt layer


def test_settings_come_from_the_environment() -> None:
    assert slack_settings({}) is None
    with pytest.raises(SlackNotConfigured):
        slack_settings({"SLACK_BOT_TOKEN": "xoxb-1"})
    settings = slack_settings({"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_SIGNING_SECRET": "s"})
    assert settings is not None and "xoxb-1" not in repr(settings)


def event(**fields: str) -> dict[str, str]:
    return {"user": "U1", "channel": "C1", "ts": "2.2", "text": "hi", **fields}


@pytest.mark.parametrize(
    ("fields", "mention", "expected"),
    [
        ({"channel_type": "im", "ts": "1.1"}, False, ("1.1", "hi", True)),
        ({"thread_ts": "1.1", "text": "<@UBOT> hi"}, True, ("1.1", "hi", True)),
        ({"thread_ts": "1.1", "text": "ok"}, False, ("1.1", "ok", False)),
        ({"text": "<@UBOT> hi"}, False, None),  # its app_mention event handles it
        ({"bot_id": "B1"}, False, None),
        ({"subtype": "message_changed"}, False, None),
    ],
)
def test_events_become_messages(fields: dict[str, str], mention: bool, expected: Any) -> None:
    got = incoming_from_event(event(**fields), "UBOT", mention=mention)
    assert (got and (got.thread_ts, got.text, got.addressed)) == (expected or None)


async def test_a_button_click_reaches_the_channel_as_a_decision() -> None:
    calls: list[dict[str, Any]] = []

    class Channel:
        async def on_decision(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    body = {
        "user": {"id": MANAGER},
        "channel": {"id": "C1"},
        "message": {"ts": "5.5", "thread_ts": "1.1"},
        "actions": [{"action_id": DECLINE_ACTION, "value": "tok"}],
    }
    await SlackListeners(Channel()).decision(body, SimpleNamespace())  # type: ignore[arg-type]
    assert calls[0] | {"poster": None} == {
        "user": MANAGER, "channel": "C1", "thread_ts": "1.1", "message_ts": "5.5",
        "token": "tok", "decision": "declined", "poster": None,
    }  # fmt: skip


def test_slack_requests_need_slack_signature(services: Services) -> None:
    app = FastAPI()
    settings = SlackSettings.model_validate({"bot_token": "xoxb-1", "signing_secret": "secret"})
    app.include_router(slack_router(settings, services))
    forged = TestClient(app).post(
        "/slack/events", json={"type": "event_callback"},
        headers={"X-Slack-Signature": "v0=bad", "X-Slack-Request-Timestamp": "1"},
    )  # fmt: skip
    assert forged.status_code == 401


@pytest.mark.parametrize("text", ["<!channel> hi", "<@U1> look <https://evil.example|here>"])
def test_model_output_cannot_ping_or_link(text: str) -> None:
    assert "<" not in escape(text) and ">" not in escape(text)


def test_the_app_serves_slack_only_when_it_is_set_up(
    env: Env, pack: Path, services: Services
) -> None:
    def answer(slack: SlackSettings | None) -> int:
        app = create_app(
            services.workspace, pack, services.store,
            auth=auth_settings({"COGRAIL_AUTH": "dev", "COGRAIL_DEV_PRINCIPAL": ALICE_ID}),
            classifier=env.classifier, provider_for=env.next_provider,
            open_registry=open_registry, slack=slack,
        )  # fmt: skip
        return TestClient(app).post("/slack/events", json={}).status_code

    settings = SlackSettings.model_validate({"bot_token": "xoxb-1", "signing_secret": "s"})
    assert answer(None) == 404  # Slack is off
    assert answer(settings) == 401  # on, and an unsigned request is refused


async def test_a_direct_message_is_opened_with_the_approver() -> None:
    opened: list[dict[str, Any]] = []

    class Client:
        async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
            opened.append(kwargs)
            return {"channel": {"id": "D9"}}

    assert await BoltPoster(cast(AsyncWebClient, Client())).open_dm(MANAGER) == "D9"
    assert opened == [{"users": MANAGER}]


async def test_posted_messages_are_not_unfurled() -> None:
    # Slack fetches a linked URL by itself, so model output must never be unfurled.
    posted: list[dict[str, Any]] = []

    class Client:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    poster = BoltPoster(cast(AsyncWebClient, Client()))
    await poster.post("C1", "1.1", "see https://example.com/?q=secret")
    assert posted[0]["unfurl_links"] is False and posted[0]["unfurl_media"] is False
