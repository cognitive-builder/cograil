"""Slack Bolt wiring: Slack events and button clicks become calls on SlackChannel.

Needs the `slack` extra. One endpoint, POST /slack/events, takes both Events API and
interactivity requests; Bolt verifies the signature with SLACK_SIGNING_SECRET before anything
else runs. The routes carry no sign-in: the signed request and the member-to-Principal map
(cograil.channels.slack.identity) are the sign-in.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request, Response
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from cograil.api.services import Services
from cograil.channels.slack.blocks import APPROVE_ACTION, DECLINE_ACTION, clip
from cograil.channels.slack.channel import ButtonDecision, Incoming, SlackChannel
from cograil.channels.slack.settings import SlackSettings

EVENTS_PATH = "/slack/events"
_MENTION = re.compile(r"<@[A-Z0-9]+>")


class BoltPoster:
    """The Poster over Slack's Web API."""

    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client

    async def post(
        self, channel: str, thread_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        await self._client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=clip(text), blocks=blocks
        )

    async def whisper(self, channel: str, thread_ts: str, user: str, text: str) -> None:
        await self._client.chat_postEphemeral(
            channel=channel, user=user, thread_ts=thread_ts, text=clip(text)
        )

    async def replace(self, channel: str, ts: str, text: str) -> None:
        await self._client.chat_update(channel=channel, ts=ts, text=text, blocks=[])


def incoming_from_event(
    event: dict[str, Any], bot_user_id: str | None, *, mention: bool
) -> Incoming | None:
    """The message in a Slack event, or None for one the bot must not act on.

    `mention` is true for an `app_mention` event. A `message` event is a direct message (acted
    on), a message that also mentions the bot (left to its `app_mention` event, so it is not
    handled twice), or a plain channel message (it only continues a Run in its thread).
    Messages from bots, edits and deletions are ignored."""
    user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
    if not user or not channel or not ts or event.get("bot_id") or event.get("subtype"):
        return None
    text = event.get("text") or ""
    direct = event.get("channel_type") == "im"
    if not mention and bot_user_id and f"<@{bot_user_id}>" in text:
        return None
    return Incoming(
        user=user,
        channel=channel,
        thread_ts=event.get("thread_ts") or ts,
        text=_MENTION.sub("", text).strip(),
        addressed=mention or direct,
    )


class SlackListeners:
    """The Bolt listeners, as methods so that they can be called without a Slack."""

    def __init__(self, channel: SlackChannel) -> None:
        self._channel = channel

    async def message(
        self, event: dict[str, Any], client: AsyncWebClient, context: Any, *, mention: bool
    ) -> None:
        incoming = incoming_from_event(
            event, getattr(context, "bot_user_id", None), mention=mention
        )
        if incoming is not None and incoming.text:
            await self._channel.on_message(incoming, BoltPoster(client))

    async def decision(self, body: dict[str, Any], client: AsyncWebClient) -> None:
        action = body["actions"][0]
        decision: ButtonDecision = (
            "approved" if action["action_id"] == APPROVE_ACTION else "declined"
        )
        message = body.get("message", {})
        channel = body["channel"]["id"]
        await self._channel.on_decision(
            user=body["user"]["id"],
            channel=channel,
            thread_ts=message.get("thread_ts") or message["ts"],
            message_ts=message["ts"],
            token=action["value"],
            decision=decision,
            poster=BoltPoster(client),
        )


def build_bolt_app(settings: SlackSettings, channel: SlackChannel) -> AsyncApp:
    app = AsyncApp(
        token=settings.bot_token.get_secret_value(),
        signing_secret=settings.signing_secret.get_secret_value(),
    )
    listeners = SlackListeners(channel)

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any], client: AsyncWebClient, context: Any) -> None:
        await listeners.message(event, client, context, mention=True)

    @app.event("message")
    async def on_message(event: dict[str, Any], client: AsyncWebClient, context: Any) -> None:
        await listeners.message(event, client, context, mention=False)

    @app.action(re.compile(f"^({APPROVE_ACTION}|{DECLINE_ACTION})$"))
    async def on_decision(ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        await ack()
        await listeners.decision(body, client)

    return app


def slack_router(settings: SlackSettings, services: Services) -> APIRouter:
    """The route Slack posts events and button clicks to."""
    handler = AsyncSlackRequestHandler(build_bolt_app(settings, SlackChannel(services)))
    router = APIRouter(tags=["slack"])

    @router.post(EVENTS_PATH, include_in_schema=False)
    async def events(request: Request) -> Response:
        return await handler.handle(request)

    return router
