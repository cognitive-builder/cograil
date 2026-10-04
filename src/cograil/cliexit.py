"""How CLI commands end: the exit codes, the one-line `fail`, and the boundary that keeps an
error Cograil did not foresee (for example a database that cannot be reached) from printing a
traceback."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, NoReturn

import typer

from cograil.domain import RunStatus

EXIT_ERROR = 1
EXIT_AWAITING_APPROVAL = 3
EXIT_ESCALATED = 4
EXIT_FAILED = 5
EXIT_BY_STATUS = {
    RunStatus.awaiting_approval: EXIT_AWAITING_APPROVAL,
    RunStatus.escalated: EXIT_ESCALATED,
    RunStatus.failed: EXIT_FAILED,
}
EXIT_CODES = """\b
Exit codes:
  0  done: the workspace is valid, or the Run completed
  1  error: invalid workspace, bad input, missing environment, a refused decision, or an
     unexpected error such as a database that cannot be reached
  2  usage error: a missing or unknown argument or option
  3  the Run is paused awaiting approval (the token is printed)
  4  the Run was escalated to the Colleague's escalation contact
  5  the Run failed (closed, with a run.failed AuditEvent)"""


def fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def await_command(body: Coroutine[Any, Any, None]) -> None:
    """Run a command's async body to its end; an error outside CograilError is one line, exit 1.

    `fail` inside the body raises typer.Exit, itself a RuntimeError, so it passes through with
    the code the command chose instead of being flattened onto exit 1.
    """
    try:
        asyncio.run(body)
    except typer.Exit:
        raise
    except Exception as exc:
        detail = str(exc).partition("\n")[0] or "no detail"  # a database error is many lines
        fail(f"error: {type(exc).__name__}: {detail}")
