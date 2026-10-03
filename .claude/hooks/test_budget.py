#!/usr/bin/env python3
"""Test budget guard for Claude Code build sessions (PreToolUse hook on Bash).

Counts test runs per session and blocks the ones beyond the budget in
.claude/test-budget.json. Limits are read from origin/main, so edits made
during a session cannot loosen them. Any internal error lets the command
through: a broken guard must never stop work. See ADR 0014.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_LIMITS = {"targeted": 20, "repeat": 5, "full": 2, "e2e": 1, "live": 0}
RANK = {"targeted": 0, "full": 1, "e2e": 2, "live": 3}
LABEL = {"targeted": "targeted test", "full": "full-suite", "e2e": "end-to-end"}
TEST_COMMAND = re.compile(r"\bpytest\b|check\.sh|\bcograil\s+eval\b")
MARKER = re.compile(r"(?<![\w-])-m\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")
VALUE_OPTIONS = {"-m", "-k", "-p", "-c", "-o", "--tb", "--maxfail", "--rootdir", "--timeout"}


def selects(term: str, cmd: str) -> bool:
    """True if any -m marker expression in cmd selects term rather than excluding it."""
    for match in MARKER.finditer(cmd):
        expr = next(group for group in match.groups() if group is not None)
        if re.search(rf"\b{term}\b", re.sub(rf"\bnot\s+{term}\b", "", expr)):
            return True
    return False


def pytest_scope(segment: str) -> str:
    """'targeted' when a pytest call names paths or -k, otherwise 'full'."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return "full"
    if "pytest" not in tokens:
        return "targeted"
    args = tokens[tokens.index("pytest") + 1 :]
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-k" or arg.startswith("-k"):
            return "targeted"
        if arg in VALUE_OPTIONS:
            skip_next = True
            continue
        if not arg.startswith("-") and ("/" in arg or arg.endswith(".py") or "::" in arg):
            return "targeted"
    return "full"


def classify(cmd: str) -> str:
    if selects("live", cmd) or re.search(r"COGRAIL_LIVE\s*=\s*[\"']?1", cmd):
        return "live"
    if selects("e2e", cmd) or "tests/e2e" in cmd or re.search(r"check\.sh\s+e2e\b", cmd):
        return "e2e"
    kinds = ["targeted"]
    if re.search(r"check\.sh\s+full\b", cmd) or selects("integration", cmd):
        kinds.append("full")
    for segment in re.split(r"&&|\|\||;|\|", cmd):
        if re.search(r"\bpytest\b", segment):
            kinds.append(pytest_scope(segment))
    return max(kinds, key=RANK.__getitem__)


def load_limits(project_dir: str) -> dict[str, int]:
    override = os.environ.get("COGRAIL_TEST_BUDGET_FILE")
    try:
        if override:
            text = Path(override).read_text()
        else:
            text = subprocess.run(
                ["git", "-C", project_dir, "show", "origin/main:.claude/test-budget.json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
        data = json.loads(text)
        return {key: int(data.get(key, value)) for key, value in DEFAULT_LIMITS.items()}
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(DEFAULT_LIMITS)


def state_file(session_id: str) -> Path:
    base = Path(os.environ.get("COGRAIL_TEST_BUDGET_STATE_DIR", tempfile.gettempdir()))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return base / f"cograil-test-budget-{safe}.json"


def verdict(kind: str, used: int, repeats: int, limits: dict[str, int]) -> str:
    if kind == "live" or limits[kind] <= 0:
        return (
            "Blocked by the test budget: tests against real models never run in build "
            "sessions. GitHub runs them on releases."
        )
    if used >= limits[kind]:
        return (
            f"Blocked by the test budget: this session has used its {limits[kind]} "
            f"{LABEL[kind]} run(s). Push the branch and let GitHub run the rest, or stop "
            "and report what is failing in the pull request."
        )
    if repeats >= limits["repeat"]:
        return (
            f"Blocked by the test budget: this exact test command has run {repeats} times "
            "this session. Change approach, or stop and report the failure in the pull request."
        )
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
        command = str(event.get("tool_input", {}).get("command", ""))
        if not TEST_COMMAND.search(command):
            return 0
        project = os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd", "."))
        limits = load_limits(project)
        kind = classify(command)
        path = state_file(str(event.get("session_id", "")))
        tally = json.loads(path.read_text()) if path.exists() else {}
        counts = tally.setdefault("counts", {})
        repeats = tally.setdefault("repeats", {})
        key = hashlib.sha1(" ".join(command.split()).encode()).hexdigest()
        reason = verdict(kind, counts.get(kind, 0), repeats.get(key, 0), limits)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        counts[kind] = counts.get(kind, 0) + 1
        repeats[key] = repeats.get(key, 0) + 1
        path.write_text(json.dumps(tally))
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
