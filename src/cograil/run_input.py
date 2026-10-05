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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    extra: Mapping[str, Any] | None = None,
    trigger: Trigger | None = None,
) -> Run:
    """A received Run with its input, a chat Run unless `trigger` says otherwise (a Schedule's
    Run); its workspace folder is recorded so that `cograil approve` can resume it. `extra`
    adds keys to the Run's context, such as the Slack thread the Run belongs to; it cannot
    replace the two keys above."""
    now = datetime.now(UTC)
    trigger = trigger or Trigger(kind="chat", channel=channel)
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
            **(extra or {}),
            "workspace_path": str(path.resolve()),
            INPUT_KEY: run_input(message, principal),
        },
    )
