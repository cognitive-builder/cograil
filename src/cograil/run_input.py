"""The input of a Run: the user's message and the requester's id, stored when the Run is received.

`Run.context["input"]` is `{"message": <text>, "requester": <principal id>}`. Every Step sees it
in its opening data block, behind the data-not-instructions preamble (rule 7), so it informs the
model and can never change a whitelist or a gate (rule 2). Only the requester's id goes in;
their groups stay in code, where entitlement is decided (rule 3).

The message is the user's own data. It is kept raw on the Run, because a resumed Run needs it
again, and is never written to an AuditEvent or a log line.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from cograil.domain import Colleague, Principal, Protocol, Run, Trigger

INPUT_KEY = "input"


def run_input(message: str, requester: Principal) -> dict[str, str]:
    """What a Run is asked: the message as the user wrote it, and who asked it."""
    return {"message": message, "requester": requester.id}


def received_run(
    workspace: str,
    path: Path,
    protocol: Protocol,
    colleague: Colleague,
    principal: Principal,
    *,
    channel: str,
    message: str,
) -> Run:
    """A received chat Run with its input; its workspace folder is recorded so that
    `cograil approve` can resume it."""
    now = datetime.now(UTC)
    trigger = Trigger(kind="chat", channel=channel)
    return Run(
        id=uuid.uuid4().hex,
        workspace=workspace,
        colleague=colleague.name,
        protocol=protocol.name,
        protocol_version=protocol.version,
        principal=principal,
        principal_id=principal.id,
        trigger=trigger,
        trigger_kind=trigger.kind,
        created_at=now,
        updated_at=now,
        context={
            "workspace_path": str(path.resolve()),
            INPUT_KEY: run_input(message, principal),
        },
    )
