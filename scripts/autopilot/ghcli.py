"""GitHub reads and writes for the autopilot, through the `gh` CLI (ADR 0015).

Only I/O lives here; the decisions are in dispatch.py. On a dry run every write is
printed instead of run. Standard library only, Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

REPO = os.environ.get("AUTOPILOT_REPO", "cognitive-builder/cograil")
LEDGER_TITLE = "Credit Ledger (not a task)"


class AutopilotError(Exception):
    """A command failed or GitHub is not in the shape the dispatcher expects."""


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutopilotError(f"{' '.join(cmd[:2])}: {exc}") from exc


class GitHub:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run

    def call(self, command: str, *extra: str, mutate: bool = False, check: bool = True) -> str:
        """Run `gh <command words> <extra args verbatim>`, against REPO unless it is `api`."""
        args = [*command.split(), *extra, *([] if command.startswith("api") else ["-R", REPO])]
        if mutate and self.dry_run:
            print(f"[dry-run] would run: gh {' '.join(args)}")
            return ""
        proc = run(["gh", *args])
        if check and proc.returncode != 0:
            raise AutopilotError(f"gh {command} exited {proc.returncode}: {proc.stderr[:300]}")
        return proc.stdout

    def json(self, command: str, *extra: str) -> Any:
        return json.loads(self.call(command, *extra) or "[]")

    def switch(self) -> str:
        """The AUTOPILOT repo variable, or '' when it is unset."""
        return self.call("variable get AUTOPILOT", check=False).strip()

    def ledger(self) -> int:
        query = f'"{LEDGER_TITLE}" in:title'
        for issue in self.json("issue list --state open --json number,title --search", query):
            if issue["title"] == LEDGER_TITLE:
                return int(issue["number"])
        raise AutopilotError(f"no open issue titled {LEDGER_TITLE!r}")

    def comments(self, issue: int) -> list[dict[str, Any]]:
        """Every comment on an issue as {id, body, assoc} (assoc is the author association)."""
        jq = ".[] | {id: .id, body: .body, assoc: .author_association}"
        out = self.call(f"api --paginate repos/{REPO}/issues/{issue}/comments --jq", jq)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def ready_issues(self) -> list[dict[str, Any]]:
        ready = "issue list --state open --label status:ready --limit 200"
        return list(self.json(f"{ready} --json number,title,labels,milestone"))

    def issue_state(self, issue: int) -> str:
        return str(json.loads(self.call(f"issue view {issue} --json state"))["state"])

    def open_prs(self) -> list[dict[str, Any]]:
        fields = "number,headRefName,labels,createdAt"
        return list(self.json(f"pr list --state open --limit 100 --json {fields}"))

    def required_checks(self, pr: int) -> list[dict[str, Any]]:
        # `gh pr checks` exits 1 when a check fails and 8 while checks are pending.
        out = self.call(f"pr checks {pr} --required --json name,bucket,completedAt", check=False)
        try:
            return list(json.loads(out or "[]"))
        except json.JSONDecodeError:  # "no required checks reported"
            return []

    def comment(self, issue: int, body: str) -> None:
        self.call(f"issue comment {issue} --body", body, mutate=True)

    def label(self, issue: int, labels: str) -> None:
        """`labels` is the gh flags, for example `--add-label needs:human`."""
        self.call(f"issue edit {issue} {labels}", mutate=True)

    def run_lane3(self, issue: int) -> str:
        return self.call(f"workflow run lane3-implement.yml -f issue={issue}", mutate=True)
