"""Scheduled triggers: a Colleague's `schedules` start Runs on a cron, as a system principal.

A `Schedule` in `colleagues/*.yaml` names a Protocol, a five-field cron (UTC), a `kind: system`
principal of `principals.yaml` and an Audience. The workspace loads only if the Protocol lists
`Scheduled execution: allowed` and the Colleague owns it (`schedule_problems`). The Run's
principal is the named system principal holding the groups of the named Audience and no others
(`scheduled_principal`), so retrieval pre-filters on them (rule 3). Every scheduled Run still
passes `check_audience` and the Tool gates; a write waits for an Approval like any other Run.

`build_scheduler` puts one APScheduler job per Schedule on the running event loop. A job that
raises a `CograilError` is logged as `schedule.failed` and the next tick still fires.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from cograil.domain import Colleague, Principal, Protocol, Schedule, Workspace
from cograil.errors import CograilError, WorkspaceError
from cograil.observability import log_event

CHANNEL = "schedule"
MISFIRE_GRACE_SECONDS = 60  # a tick the loop was too busy to start within a minute is dropped
_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")  # cron: 0 and 7 are Sunday
_WEEKDAY_NUMBER = re.compile(r"(?<![/\d])[0-7](?!\d)")

type Fire = Callable[[Colleague, Schedule], Awaitable[object]]


def cron_trigger(cron: str) -> CronTrigger:
    """The APScheduler trigger for a five-field cron in UTC; ValueError if it is not one.

    Weekday numbers mean what they mean in cron (1 is Monday, 0 and 7 Sunday); APScheduler 3
    counts from Monday as 0, so they are written as names before it reads them."""
    fields = cron.split()
    if len(fields) == 5:
        fields[4] = _WEEKDAY_NUMBER.sub(lambda m: _WEEKDAYS[int(m.group())], fields[4])
    return CronTrigger.from_crontab(" ".join(fields), timezone=UTC)


def schedules(workspace: Workspace) -> list[tuple[Colleague, Schedule]]:
    """Every Schedule of the workspace with the Colleague that owns it, in workspace order."""
    return [(c, s) for c in workspace.colleagues for s in c.schedules]


def scheduled_principal(workspace: Workspace, schedule: Schedule) -> Principal:
    """The system principal the Schedule runs as, holding its Audience's groups."""
    system = next(
        (p for p in workspace.principals if p.id == schedule.principal and p.kind == "system"),
        None,
    )
    audience = next((a for a in workspace.audiences if a.name == schedule.audience), None)
    if system is None or audience is None:
        raise WorkspaceError(
            f"schedule {schedule.name}: needs system principal {schedule.principal} "
            f"and audience {schedule.audience}"
        )
    return system.model_copy(update={"groups": sorted(set(audience.groups))})


def schedule_problems(workspace: Workspace, folder: str) -> list[str]:
    """Why a Schedule cannot run, one line each, naming `folder` (the colleagues folder)."""
    protocols = {p.name: p for p in workspace.protocols}
    systems = {p.id: p for p in workspace.principals if p.kind == "system"}
    audiences = {a.name for a in workspace.audiences}
    found: list[str] = []
    for colleague, schedule in schedules(workspace):
        who = f"{folder}: {colleague.name} schedule {schedule.name}"
        found.extend(f"{who}: {why}" for why in _denials(
            colleague, schedule, protocols, systems, audiences
        ))  # fmt: skip
    return found


def _denials(
    colleague: Colleague,
    schedule: Schedule,
    protocols: dict[str, Protocol],
    systems: dict[str, Principal],
    audiences: set[str],
) -> list[str]:
    why: list[str] = []
    protocol = protocols.get(schedule.protocol)
    if schedule.protocol not in colleague.protocols or protocol is None:
        why.append(f"protocol {schedule.protocol} is not one of the colleague's protocols")
    elif not protocol.scheduled_allowed:
        why.append(f"protocol {schedule.protocol} does not allow scheduled execution")
    if sum(s.name == schedule.name for s in colleague.schedules) > 1:
        why.append("the name is used twice")
    try:
        cron_trigger(schedule.cron)
    except ValueError as exc:
        why.append(f"invalid cron {schedule.cron!r}: {exc}")
    system = systems.get(schedule.principal)
    if system is None:
        why.append(f"{schedule.principal} is not a system principal in principals.yaml")
    elif system.groups:
        why.append(f"{system.id} takes its groups from the audience; drop its own groups")
    if schedule.audience not in audiences:
        why.append(f"audience {schedule.audience} is not in audiences.yaml")
    return why


def build_scheduler(workspace: Workspace, fire: Fire) -> AsyncIOScheduler:
    """A scheduler with a job per Schedule that calls `fire`; the caller starts and stops it."""
    scheduler = AsyncIOScheduler(timezone=UTC)
    for colleague, schedule in schedules(workspace):
        scheduler.add_job(
            _fire,
            cron_trigger(schedule.cron),
            args=[fire, colleague, schedule],
            id=f"{colleague.name}/{schedule.name}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
    return scheduler


async def _fire(fire: Fire, colleague: Colleague, schedule: Schedule) -> None:
    try:
        await fire(colleague, schedule)
    except CograilError as exc:
        log_event(
            "schedule.failed",
            colleague=colleague.name,
            schedule=schedule.name,
            principal_id=schedule.principal,
            error=f"{type(exc).__name__}: {exc}",
        )
