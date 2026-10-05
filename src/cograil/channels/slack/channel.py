"""What the Slack channel does with a message and with a button, independent of Bolt.

A message that mentions the bot, or is sent to it directly, starts a Run through
`Services.chat`; the Run remembers its Slack thread in `Run.context["slack"]`, and every reply
about it goes to that thread, so there is one thread per Run. A later message in that thread
continues the Run: it answers where the Run stands and, for a Run paused at a gate, repeats whom
the Run waits for; the approver is never sent the prompt a second time. The Run itself only
moves on a decision (rule 1), never on chat.

A gate's prompt goes to its approver, not to the Run's thread: a direct message with the
approver's Slack member id (`slack_id` in principals.yaml, reversed) that names the requester, the
Tool, the Step and the call's arguments. The Run's thread only says who it waits for. An approver
with no `slack_id`, or one Slack will not deliver the prompt to, gets nothing in Slack and the
thread says which it was and where they decide.

An approval button decides through `Services.decide`, the code behind POST /approvals/{token},
as the Principal the clicking Slack member maps to: the Runner refuses anyone but the
approver and writes the AuditEvent, with `via: slack`. Nothing in a message or a button can
name another decider (rule 2). What replaces the prompt is built from the Approval the Runner
recorded, and a Run that fails after an accepted decision says so in its thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from cograil.api.services import Services
from cograil.api.sse import error_body
from cograil.channels.slack.blocks import (
    approval_blocks,
    decided_text,
    no_slack_approver_text,
    outcome_text,
    undelivered_prompt_text,
    waiting_text,
)
from cograil.channels.slack.identity import principal_for_slack_user, slack_id_of, slack_mention
from cograil.domain import Approval, Principal, Run, RunStatus
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
# Errors that turn a decision away at the gate; anything else came after it was accepted.
_TURNED_AWAY = (
    ApprovalNotAllowed, ApprovalNotFound, ApprovalAlreadyDecided, RunNotFound, RunNotPaused,
    RunClaimLost, WorkspaceError,
)  # fmt: skip
type ButtonDecision = Literal["approved", "declined"]


class Poster(Protocol):
    """How the channel speaks in Slack; BoltPoster implements it."""

    async def post(
        self,
        channel: str,
        thread_ts: str | None,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None: ...

    async def open_dm(self, user: str) -> str:
        """The id of the direct message conversation with `user`."""
        ...

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
        decision: ButtonDecision,
        poster: Poster,
    ) -> None:
        """Decide the Approval `token` as the Principal `user` maps to. `channel` is where the
        click was, the approver's direct message; the Run's own thread is in the Run."""
        with self._tracked():
            principal = self._principal(user)
            if principal is None:
                await poster.whisper(channel, thread_ts, user, UNKNOWN_MEMBER.format(user=user))
                return
            try:
                await self._services.decide(token, principal, decision, via="slack")
            except Exception as exc:  # a refusal is whispered; a Run that failed is announced
                # Catching everything is safe: `_decided` returns None while the Approval is
                # still pending, so an unexpected failure is never announced as a decision.
                await self._decision_failed(
                    exc, user, (channel, thread_ts, message_ts), token, poster
                )
                return
            approval = await self._services.store.get_approval(token)
            await poster.replace(channel, message_ts, decided_text(approval, user))
            run = await self._services.store.get_run(approval.run_id)
            await self._report(run.id, *_thread_of(run, channel, thread_ts), poster)

    async def _decision_failed(
        self, exc: Exception, user: str, click: tuple[str, str, str], token: str, poster: Poster
    ) -> None:
        """A decision that raised: whisper a refusal to the clicker; but when the Runner had
        accepted it and the Run then failed, replace the prompt and tell the Run's thread."""
        channel, thread_ts, message_ts = click
        approval = await self._decided(token) if not isinstance(exc, _TURNED_AWAY) else None
        if approval is None:
            await poster.whisper(channel, thread_ts, user, _refusal(exc))
            return
        log_event("slack.decision_failed", logging.ERROR, error=type(exc).__name__)
        run = await self._services.store.get_run(approval.run_id)
        await poster.replace(channel, message_ts, decided_text(approval, user))
        where = _thread_of(run, channel, thread_ts)
        await poster.post(
            *where, f"This run {run.status.value} after the decision. The audit log says why."
        )

    async def _decided(self, token: str) -> Approval | None:
        """The Approval once it is decided, None while it is still pending (or gone)."""
        try:
            approval = await self._services.store.get_approval(token)
        except ApprovalNotFound:
            return None
        return None if approval.decision == "pending" else approval

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
        """A message in a Run's thread: repeat whom a pending gate waits for, or say where the
        Run stands."""
        if run.status is RunStatus.awaiting_approval:
            await self._report(run.id, message.channel, message.thread_ts, poster, prompt=False)
            return
        if run.status in _WORKING:
            reply = "This run is still working. I'll post here when it stops."
        else:
            status = run.status.value.replace("_", " ")
            reply = f"This run is {status}. Message me in a new thread to start another."
        await poster.post(message.channel, message.thread_ts, reply)

    async def _report(
        self, run_id: str, channel: str, thread_ts: str, poster: Poster, *, prompt: bool = True
    ) -> None:
        """Tell the Run's thread where it stands: its answer, or who each pending gate waits
        for. With `prompt`, each approver is also sent the gate; a message asking again is not
        a reason to message the approver again."""
        run = await self._services.store.get_run(run_id)
        outcome = await self._services.outcome(run)
        if (text := outcome_text(outcome)) is not None:
            await poster.post(channel, thread_ts, text)
        for pending in outcome.awaiting:
            approval = await self._services.store.get_approval(pending.token)
            await self._deliver(run, approval, (channel, thread_ts), poster, prompt=prompt)

    async def _deliver(
        self, run: Run, approval: Approval, thread: tuple[str, str], poster: Poster, *, prompt: bool
    ) -> None:
        """Say in the Run's thread whom a pending gate waits for, and with `prompt` also send
        the gate to its approver in a direct message. An approver with no `slack_id`, or one
        Slack will not deliver to, is sent nothing; the thread says where they decide."""
        workspace = self._services.workspace
        approver = slack_mention(workspace, approval.approver)
        slack_id = slack_id_of(workspace, approval.approver)
        if slack_id is None:
            await poster.post(*thread, no_slack_approver_text(approver))
            return
        try:
            # Opened even without `prompt`: whether Slack answers says if the approver is still
            # reachable, without sending them the prompt a second time.
            dm = await poster.open_dm(slack_id)
            if prompt:
                blocks = approval_blocks(approval, slack_mention(workspace, run.principal_id))
                await poster.post(dm, None, f"Approval needed for {approval.tool}", blocks)
        except Exception as exc:  # Slack refused (a missing scope, a deactivated member)
            log_event("slack.prompt_undelivered", logging.WARNING, error=type(exc).__name__)
            await poster.post(*thread, undelivered_prompt_text(approver))
            return
        await poster.post(*thread, waiting_text(approver))

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


def _thread_of(run: Run, channel: str, thread_ts: str) -> tuple[str, str]:
    """The Slack thread the Run belongs to; where the click was, for a Run that has none."""
    here = run.context.get(SLACK_KEY) or {}
    return here.get("channel", channel), here.get("thread_ts", thread_ts)


def _refusal(exc: Exception) -> str:
    """What a member is told when their decision is refused."""
    if not isinstance(exc, CograilError):
        return "That could not be completed."
    if isinstance(exc, ApprovalNotAllowed):
        return "You are not the approver of this request."
    if isinstance(exc, ApprovalNotFound | RunNotFound):
        return "That approval no longer exists."
    if isinstance(exc, ApprovalAlreadyDecided):
        return "That approval was already decided."
    if isinstance(exc, RunNotPaused | RunClaimLost | WorkspaceError):
        return str(exc)
    return error_body(exc)["message"]
