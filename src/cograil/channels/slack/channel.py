"""What the Slack channel does with a message and with a button, independent of Bolt.

A message that mentions the bot, or is sent to it directly, starts a Run through
`Services.chat`; the Run remembers its Slack thread in `Run.context["slack"]`, and every reply
about it goes to that thread, so there is one thread per Run. A later message in that thread
continues the Run: it answers where the Run stands and, for a Run paused at a gate, asks again.
The Run itself only moves on a decision (rule 1), never on chat.

An approval button decides through `Services.decide`, the code behind POST /approvals/{token},
as the Principal the clicking Slack member maps to: the Runner refuses anyone but the
approver and writes the AuditEvent, with `via: slack`. Nothing in a message or a button can
name another decider (rule 2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from cograil.api.schemas import RunOutcome
from cograil.api.services import Services
from cograil.api.sse import error_body
from cograil.channels.slack.blocks import approval_blocks, outcome_text
from cograil.channels.slack.identity import principal_for_slack_user, slack_mention
from cograil.domain import Principal, Run, RunStatus
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
from cograil.observability import log_event
from cograil.redaction import redact_patterns

SLACK_KEY = "slack"
CHANNEL = "slack"
THREAD_SCAN = 50  # how many of a principal's newest Runs are searched for a thread
UNKNOWN_MEMBER = (
    "I don't know your Slack account yet. Ask your workspace admin to add your Slack member id "
    "({user}) to principals.yaml."
)
_WORKING = {RunStatus.received, RunStatus.planned, RunStatus.running}
type Decision = Literal["approved", "declined"]


class Poster(Protocol):
    """How the channel speaks in Slack; BoltPoster implements it."""

    async def post(
        self, channel: str, thread_ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None: ...

    async def whisper(self, channel: str, thread_ts: str, user: str, text: str) -> None: ...

    async def replace(self, channel: str, ts: str, text: str) -> None: ...


@dataclass(frozen=True)
class Incoming:
    """A message to the bot. `addressed` is true for a direct message or an @mention; a plain
    message in a channel only continues a Run whose thread it is in."""

    user: str
    channel: str
    thread_ts: str
    text: str
    addressed: bool


class SlackChannel:
    def __init__(self, services: Services) -> None:
        self._services = services

    async def on_message(self, message: Incoming, poster: Poster) -> None:
        """Start a Run for the message, or continue the Run whose thread it is in."""
        with self._tracked():
            principal = self._principal(message.user)
            if principal is None:
                if message.addressed:
                    reply = UNKNOWN_MEMBER.format(user=message.user)
                    await poster.post(message.channel, message.thread_ts, reply)
                return
            run = await self._thread_run(principal, message.channel, message.thread_ts)
            if run is not None:
                await self._continue(run, message, poster)
            elif message.addressed:
                await self._start(principal, message, poster)

    async def on_decision(
        self,
        user: str,
        channel: str,
        thread_ts: str,
        message_ts: str,
        token: str,
        decision: Decision,
        poster: Poster,
    ) -> None:
        """Decide the Approval `token` as the Principal `user` maps to."""
        with self._tracked():
            principal = self._principal(user)
            if principal is None:
                await poster.whisper(channel, thread_ts, user, UNKNOWN_MEMBER.format(user=user))
                return
            try:
                outcome = await self._services.decide(token, principal, decision, via="slack")
            except CograilError as exc:
                await poster.whisper(channel, thread_ts, user, _refusal(exc))
                return
            verb = "Approved" if decision == "approved" else "Declined"
            await poster.replace(channel, message_ts, f"{verb} by <@{user}>.")
            await self._report(outcome.run.id, channel, thread_ts, poster)

    def _principal(self, slack_id: str) -> Principal | None:
        principal = principal_for_slack_user(self._services.workspace, slack_id)
        if principal is None:
            log_event("slack.unmapped_member", logging.WARNING, member=slack_id)
        return principal

    async def _thread_run(self, principal: Principal, channel: str, thread_ts: str) -> Run | None:
        """The principal's Run that owns this thread, found among their newest Runs."""
        here = {"channel": channel, "thread_ts": thread_ts}
        runs = await self._services.store.list_runs(THREAD_SCAN, principal_id=principal.id)
        return next((r for r in runs if r.context.get(SLACK_KEY) == here), None)

    async def _start(self, principal: Principal, message: Incoming, poster: Poster) -> None:
        seen: dict[str, dict[str, Any]] = {}
        here = {"channel": message.channel, "thread_ts": message.thread_ts}
        try:
            await self._services.chat(
                principal,
                message.text,
                lambda event, data: seen.__setitem__(event, data),
                channel=CHANNEL,
                context={SLACK_KEY: here},
            )
        except CograilError as exc:
            await poster.post(message.channel, message.thread_ts, error_body(exc)["message"])
            return
        except Exception as exc:  # the thread must be told; details stay in the logs
            log_event(
                "slack.chat_failed", logging.ERROR,
                error=type(exc).__name__, detail=redact_patterns(str(exc)),
            )  # fmt: skip
            await poster.post(message.channel, message.thread_ts, "That could not be completed.")
            return
        if "refusal" in seen:
            await poster.post(message.channel, message.thread_ts, seen["refusal"]["text"] or "…")
        elif "run" in seen:
            await self._report(seen["run"]["run_id"], message.channel, message.thread_ts, poster)

    async def _continue(self, run: Run, message: Incoming, poster: Poster) -> None:
        """A message in a Run's thread: re-ask a pending gate, or say where the Run stands."""
        if run.status is RunStatus.awaiting_approval:
            await self._report(run.id, message.channel, message.thread_ts, poster)
            return
        if run.status in _WORKING:
            reply = "This run is still working. I'll post here when it stops."
        else:
            status = run.status.value.replace("_", " ")
            reply = f"This run is {status}. Message me in a new thread to start another."
        await poster.post(message.channel, message.thread_ts, reply)

    async def _report(self, run_id: str, channel: str, thread_ts: str, poster: Poster) -> None:
        """Tell the thread where the Run stands: its answer, or a prompt for each pending gate.
        A Run's thread is its principal's chat, so every pending gate is shown (as /chat does)."""
        run = await self._services.store.get_run(run_id)
        outcome = await self._services.outcome(run)
        if (text := outcome_text(outcome)) is not None:
            await poster.post(channel, thread_ts, text)
        await self._prompt_approvals(outcome, channel, thread_ts, poster)

    async def _prompt_approvals(
        self, outcome: RunOutcome, channel: str, thread_ts: str, poster: Poster
    ) -> None:
        for approval in outcome.awaiting:
            mention = slack_mention(self._services.workspace, approval.approver)
            blocks = approval_blocks(approval, mention)
            await poster.post(channel, thread_ts, f"Approval needed for {approval.tool}", blocks)

    @contextmanager
    def _tracked(self) -> Iterator[None]:
        """Keep the running task in `Services.tasks`, so shutdown lets a Run finish."""
        task = asyncio.current_task()
        if task is not None:
            self._services.tasks.add(task)
        try:
            yield
        finally:
            if task is not None:
                self._services.tasks.discard(task)


def _refusal(exc: CograilError) -> str:
    """What a member is told when their decision is refused."""
    if isinstance(exc, ApprovalNotAllowed):
        return "You are not the approver of this request."
    if isinstance(exc, ApprovalNotFound | RunNotFound):
        return "That approval no longer exists."
    if isinstance(exc, ApprovalAlreadyDecided):
        return "That approval was already decided."
    if isinstance(exc, RunNotPaused | RunClaimLost | WorkspaceError):
        return str(exc)
    return error_body(exc)["message"]
