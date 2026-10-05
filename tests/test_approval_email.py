"""Approval by email link (issue #24): the email, the single-use link, expiry and the audit."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from test_api import (
    ALICE,
    MANAGER,
    REST_SCRIPT,
    Env,
    kinds,
    stored,
)

from cograil.api.approval_mail import ApprovalMail
from cograil.approval_links import ApprovalLinks
from cograil.domain import RunStatus
from cograil.errors import EmailDeliveryError


class Outbox:
    """An EmailSender that keeps what it was given, or refuses it."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[EmailMessage] = []
        self._fail = fail

    async def send(self, message: EmailMessage) -> None:
        if self._fail:
            raise EmailDeliveryError("smtp: refused")
        self.sent.append(message)


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
def env(tmp_path: Path, outbox: Outbox) -> Env:
    links = ApprovalLinks("k" * 32, "https://cograil.example.com")
    return Env(tmp_path, approval_mail=ApprovalMail(outbox, "cograil@example.com", links))


def link_of(mail: EmailMessage) -> str:
    found = re.search(r"https://\S+", mail.get_content())
    assert found
    return found.group(0)


def local(link: str) -> str:
    url = urlparse(link)
    return f"{url.path}?{url.query}"


def audit(env: Env, run_id: str, kind: str) -> list[Any]:
    return [e for e in asyncio.run(env.store.list_audit_events(run_id)) if e.kind == kind]


def test_the_approver_gets_an_email_with_a_signed_link(env: Env, outbox: Outbox) -> None:
    done = env.paused_run()
    (mail,) = outbox.sent
    assert (mail["To"], mail["From"]) == (MANAGER, "cograil@example.com")
    assert "demo.record" in mail.get_content() and ALICE in mail.get_content()
    token = done["awaiting"][0]["token"]
    assert link_of(mail).startswith(f"https://cograil.example.com/approvals/link/{token}?exp=")
    (event,) = audit(env, done["run"]["id"], "approval.emailed")
    assert (event.detail["approver"], event.detail["token"]) == (MANAGER, token)
    assert "sig" not in str(event.detail)  # the link is a credential, never audited


def test_a_failed_email_is_audited_and_does_not_fail_the_run(tmp_path: Path) -> None:
    links = ApprovalLinks("k" * 32, "https://x.example")
    env = Env(tmp_path, approval_mail=ApprovalMail(Outbox(fail=True), "a@x.io", links))
    done = env.paused_run()
    assert done["run"]["status"] == "awaiting_approval"
    assert kinds(env, done["run"]["id"]).count("approval.email_failed") == 1


def test_an_approval_is_emailed_once_however_often_the_approvers_are_notified(
    env: Env, outbox: Outbox
) -> None:
    done = env.paused_run()
    services = env.app.state.cograil_services
    run = stored(env, done["run"]["id"])
    assert asyncio.run(services.notify_approvers(run)).status is RunStatus.awaiting_approval
    assert len(outbox.sent) == 1


def test_a_chat_run_with_no_channel_to_its_approver_is_recorded_not_escalated(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)  # no mail, no Slack: the requester sees the pending approval in the chat
    done = env.paused_run()
    run_id = done["run"]["id"]
    assert done["run"]["status"] == "awaiting_approval"
    assert kinds(env, run_id).count("approval.undeliverable") == 1


def test_opening_the_link_shows_the_call_and_decides_nothing(env: Env, outbox: Outbox) -> None:
    done = env.paused_run()
    page = env.client.get(local(link_of(outbox.sent[0])))
    assert page.status_code == 200 and "demo.record" in page.text
    assert stored(env, done["run"]["id"]).status is RunStatus.awaiting_approval


def test_posting_the_link_approves_once_and_audits_the_approver(env: Env, outbox: Outbox) -> None:
    done = env.paused_run()
    run_id = done["run"]["id"]
    env.run_scripts.append(env.script(REST_SCRIPT))
    url = local(link_of(outbox.sent[0]))
    response = env.client.post(url, json={"decision": "approved"})
    assert response.status_code == 200, response.text
    assert stored(env, run_id).status is RunStatus.completed
    (resumed,) = audit(env, run_id, "gate.resumed")
    assert (resumed.detail["decided_by"], resumed.detail["via"]) == (MANAGER, "email_link")
    # Single use: the Approval is decided, so the same link decides nothing more.
    assert env.client.post(url, json={"decision": "declined"}).status_code == 409
    assert env.client.get(url).status_code == 409


def test_a_declined_link_escalates(env: Env, outbox: Outbox) -> None:
    run_id = env.paused_run()["run"]["id"]
    url = local(link_of(outbox.sent[0]))
    assert env.client.post(url, json={"decision": "declined"}).status_code == 200
    assert stored(env, run_id).status is RunStatus.escalated
    assert audit(env, run_id, "run.escalated")[0].detail["via"] == "email_link"


@pytest.mark.parametrize("tamper", ["sig", "exp", "token", "sig_non_ascii"])
def test_a_forged_link_is_refused_and_changes_nothing(
    env: Env, outbox: Outbox, tamper: str
) -> None:
    run_id = env.paused_run()["run"]["id"]
    url = local(link_of(outbox.sent[0]))
    if tamper == "sig":
        url = url[:-1] + ("0" if url[-1] != "0" else "1")
    elif tamper == "sig_non_ascii":
        url = re.sub(r"sig=\w+", "sig=%C3%A9", url)
    elif tamper == "exp":
        url = re.sub(r"exp=(\d+)", lambda m: f"exp={int(m.group(1)) + 999}", url)
    else:
        url = re.sub(r"link/[0-9a-f]+", "link/" + "0" * 32, url)
    assert env.client.post(url, json={"decision": "approved"}).status_code == 403
    assert env.client.get(url).status_code == 403
    assert stored(env, run_id).status is RunStatus.awaiting_approval


def test_an_expired_link_escalates_the_run(env: Env, outbox: Outbox) -> None:
    run_id = env.paused_run()["run"]["id"]
    token = next(iter(env.store._approvals))  # type: ignore[attr-defined]
    past = datetime.now(UTC) - timedelta(minutes=1)
    expired = env.store._approvals[token].model_copy(update={"expires_at": past})  # type: ignore[attr-defined]
    env.store._approvals[token] = expired  # type: ignore[attr-defined]
    url = local(env.app.state.cograil_services.approval_mail.links.link(expired))
    response = env.client.post(url, json={"decision": "approved"})
    assert response.status_code == 410
    assert stored(env, run_id).status is RunStatus.escalated
    assert audit(env, run_id, "run.escalated")[0].detail["reason"] == "approval_expired"
    assert env.client.get(url).status_code == 410  # escalated already; nothing more to decide


def overdue_link(env: Env) -> tuple[str, str]:
    """Put the paused Run's Approval past its expiry; its run id and the link signed for it."""
    run_id = env.paused_run()["run"]["id"]
    return run_id, reissued(env, expires_at=datetime.now(UTC) - timedelta(minutes=1))


def snapshot(env: Env, run_id: str) -> tuple[Any, Any, Any]:
    token = next(iter(env.store._approvals))  # type: ignore[attr-defined]
    return (
        stored(env, run_id),
        asyncio.run(env.store.get_approval(token)),
        asyncio.run(env.store.list_audit_events(run_id)),
    )


def test_a_get_of_an_expired_link_changes_nothing(env: Env) -> None:
    run_id, url = overdue_link(env)
    before = snapshot(env, run_id)
    assert env.client.get(url).status_code == 410
    assert snapshot(env, run_id) == before
    assert before[0].status is RunStatus.awaiting_approval


def test_the_sweep_escalates_an_overdue_approval_without_anyone_opening_the_link(
    env: Env,
) -> None:
    run_id, url = overdue_link(env)
    services = env.app.state.cograil_services
    token = next(iter(env.store._approvals))  # type: ignore[attr-defined]
    assert asyncio.run(services.sweep_overdue_approvals()) == [token]
    assert stored(env, run_id).status is RunStatus.escalated
    assert asyncio.run(env.store.get_approval(token)).decision == "expired"
    (event,) = audit(env, run_id, "run.escalated")
    assert event.detail["reason"] == "approval_expired"
    assert asyncio.run(services.sweep_overdue_approvals()) == []  # nothing left to escalate
    assert env.client.get(url).status_code == 410  # the link now says it expired, not 409


def test_the_sweep_leaves_an_approval_that_is_not_yet_due(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    assert asyncio.run(env.app.state.cograil_services.sweep_overdue_approvals()) == []
    assert stored(env, run_id).status is RunStatus.awaiting_approval


def test_the_sweep_leaves_another_workspaces_approval_alone(env: Env) -> None:
    run_id, _ = overdue_link(env)
    foreign = stored(env, run_id).model_copy(update={"workspace": "elsewhere"})
    env.store._runs[run_id] = foreign  # type: ignore[attr-defined]
    assert asyncio.run(env.app.state.cograil_services.sweep_overdue_approvals()) == []
    assert stored(env, run_id).status is RunStatus.awaiting_approval


def test_the_app_sweeps_at_startup_and_stops_the_sweep_at_shutdown(env: Env) -> None:
    run_id, _ = overdue_link(env)

    async def start_and_stop() -> Any:
        async with env.app.router.lifespan_context(env.app):
            sweeper = env.app.state.cograil_sweeper
            await asyncio.sleep(0.2)  # the first sweep runs as the app starts
            assert not sweeper.done()
        return sweeper

    sweeper = asyncio.run(start_and_stop())
    assert sweeper.cancelled()
    assert stored(env, run_id).status is RunStatus.escalated


def test_the_link_routes_are_off_without_approval_mail(tmp_path: Path) -> None:
    plain = Env(tmp_path)
    assert plain.client.get("/approvals/link/abc?exp=1&sig=x").status_code == 404


def reissued(env: Env, **changes: Any) -> str:
    """A link the service itself would sign for the stored Approval after `changes` to it."""
    token = next(iter(env.store._approvals))  # type: ignore[attr-defined]
    changed = env.store._approvals[token].model_copy(update=changes)  # type: ignore[attr-defined]
    env.store._approvals[token] = changed  # type: ignore[attr-defined]
    return local(env.app.state.cograil_services.approval_mail.links.link(changed))


def test_the_link_never_lets_the_runs_own_principal_approve(env: Env) -> None:
    run_id = env.paused_run()["run"]["id"]
    url = reissued(env, approver=ALICE)  # the Run's principal, as approver
    response = env.client.post(url, json={"decision": "approved"})
    assert response.status_code == 403 and ALICE not in response.text
    assert stored(env, run_id).status is RunStatus.awaiting_approval
    assert audit(env, run_id, "gate.refused")[0].detail["reason"] == "not_approver"


def test_the_link_cannot_approve_a_run_started_under_another_harness(
    env: Env, outbox: Outbox
) -> None:
    run_id = env.paused_run()["run"]["id"]
    services = env.app.state.cograil_services
    harness = services.workspace.harness
    services.workspace.harness = harness.model_copy(update={"version": "9.0.0"})
    response = env.client.post(local(link_of(outbox.sent[0])), json={"decision": "approved"})
    assert response.status_code == 409
    assert stored(env, run_id).status is RunStatus.awaiting_approval
    assert audit(env, run_id, "gate.refused")[0].detail["reason"] == "run_version_changed"
