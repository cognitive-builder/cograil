"""What a Run was started with: its harness version and tool pack version.

`Runner.run` stamps both on the Run: the harness version (ADR 0012) and the registry's tool
pack version (registry.py, issue #110). An approval runs Tool code under them, so
`Gates.resume` refuses one when either has changed since, after the approver check and with a
`gate.refused` AuditEvent naming the decider; the Run has to be restarted (issue #97). A
decline runs no Tool code and goes through, escalating as usual. `Runner.run` refuses a Run
left running that was stamped with other versions, rather than re-stamping it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cograil.domain import Run
from cograil.errors import HarnessChanged, RunVersionChanged, ToolPackChanged
from cograil.observability import log_event


@dataclass(frozen=True)
class RunVersions:
    """The versions the workspace has now, to compare with the ones stamped on a Run."""

    harness_version: str
    tool_pack_version: str

    def changes(self, run: Run) -> dict[str, dict[str, str]]:
        """Each version that differs from the Run's: {name: {"run": ..., "now": ...}}."""
        stamped = {"harness_version": run.harness_version,
                   "tool_pack_version": run.tool_pack_version}  # fmt: skip
        now = {"harness_version": self.harness_version, "tool_pack_version": self.tool_pack_version}
        return {
            name: {"run": stamped[name], "now": now[name]}
            for name in now
            if stamped[name] != now[name]
        }

    def refusal(self, run: Run) -> RunVersionChanged | None:
        """The error refusing this Run, or None if it runs under the versions it started with.

        A changed tool pack is named first: it changes the code an approved write runs."""
        changes = self.changes(run)
        if not changes:
            return None
        log_event("run.version_changed", logging.WARNING, run_id=run.id, changed=sorted(changes))
        said = "; ".join(
            f"{name.removesuffix('_version').replace('_', ' ')} was {seen['run']}, "
            f"now {seen['now']}"
            for name, seen in changes.items()
        )
        message = f"run {run.id} started under another workspace ({said}); restart it"
        if "tool_pack_version" in changes:
            return ToolPackChanged(message, changes)
        return HarnessChanged(message, changes)

    def require(self, run: Run) -> None:
        """Raise ToolPackChanged or HarnessChanged unless the Run started with these."""
        refusal = self.refusal(run)
        if refusal is not None:
            raise refusal
