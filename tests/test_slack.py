"""The Slack channel (issue #27): messages start or continue a Run in one thread, approval
buttons decide as the Principal the clicking member maps to, and members map to principals
through principals.yaml. A fake Poster stands in for Slack; no test calls Slack."""

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
from cograil.channels.slack.blocks import APPROVE_ACTION, DECLINE_ACTION
from cograil.channels.slack.bolt import SlackListeners, incoming_from_event, slack_router
from cograil.channels.slack.identity import escape
from cograil.domain import RunStatus
from cograil.errors import SlackNotConfigured, WorkspaceError
from cograil.workspace import load_workspace

ALICE, MANAGER, OUTSIDER = "U_ALICE", "U_MANAGER", "U_OUTSIDER"
DM = "D1"
ALICE_ID = "alice@example.com"


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.whispers: list[dict[str, Any]] = []
        self.replaced: list[dict[str, Any]] = []

    async def post(
        self, channel: str, thread_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        self.posts.append(
            {"channel": channel, "thread_ts": thread_ts, "text": text, "blocks": blocks}
        )

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
    await channel.on_decision(user, DM, "100.1", "100.2", poster.token(), decision, poster)


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
    assert {p["thread_ts"] for p in poster.posts} == {"100.1"}
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
    assert len(poster.buttons()) == 4  # the gate is asked again, in the same thread
    assert {p["thread_ts"] for p in poster.posts} == {"100.1"}


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
    assert poster.replaced == [{"ts": "100.2", "text": f"Approved by <@{MANAGER}>."}]
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
