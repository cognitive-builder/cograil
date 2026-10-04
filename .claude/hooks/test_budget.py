#!/usr/bin/env python3
"""Test budget guard for Claude Code build sessions (PreToolUse hook on Bash).

Counts every test run in a command, per session, and blocks the ones beyond the
budget in .claude/test-budget.json. Limits are read from origin/main, so edits
made during a session cannot loosen them. Loops around tests are refused, and an
unreadable tally blocks instead of resetting. A crash in the guard itself lets
the command through, so a guard bug never stops work. See ADR 0014 for the
limits of what a command-text guard can see.
"""

from __future__ import annotations

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
PYTEST_RUN = r"\bpytest\b(?![.\w-])"
TEST_COMMAND = re.compile(PYTEST_RUN + r"|\bpytest\.main\b|check\.sh|\bcograil\s+eval\b|\bptw\b")
IMPORT = re.compile(r"\b(?:import|from)\s+pytest\b")
SEPARATOR = re.compile(r"&&|\|\||[;|&\n]")
LOOP = re.compile(
    r"\b(?:for|while|until)\b[^;\n]*[;\n]\s*do\b|\bxargs\b|\bparallel\b|\bwatch\b|\bptw\b"
    r"|--count\b|--looponfail\b"
)
LIVE_ENV = re.compile(r"COGRAIL_LIVE\s*=\s*[\"']?(?:1|true|yes|on)\b", re.IGNORECASE)
CALL_MARKERS = re.compile(r"[\"']-m[\"']\s*,\s*[\"']([^\"']*)[\"']|[\"']-m([^\"'\s]+)[\"']")
VALUE_OPTIONS = {"-m", "-k", "-p", "-c", "-o", "--tb", "--maxfail", "--rootdir", "--timeout"}

BLOCK_LIVE = (
    "Blocked by the test budget: tests against real models never run in build sessions. "
    "GitHub runs them on releases."
)
BLOCK_LOOP = (
    "Blocked by the test budget: tests run one command at a time, never in a loop or repeat "
    "mode, so every run can be counted."
)
BLOCK_TALLY = (
    "Blocked by the test budget: this session's tally file is unreadable, so the budget cannot "
    "be checked. Stop and report this in the pull request."
)


def split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def marker_expressions(tokens: list[str]) -> list[str]:
    """Every -m expression in a token list, in '-m expr' and attached '-mexpr' forms."""
    found: list[str] = []
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            found.append(tokens[index + 1])
        elif token.startswith("-m") and not token.startswith("--") and len(token) > 2:
            found.append(token[2:].lstrip("="))
        elif token.startswith("PYTEST_ADDOPTS="):
            found.extend(marker_expressions(split(token.split("=", 1)[1])))
    return found


def selects(term: str, expressions: list[str]) -> bool:
    """True if any marker expression selects term rather than only excluding it."""
    for expr in expressions:
        if re.search(rf"\b{term}\b", re.sub(rf"\bnot\s+{term}\b", "", expr)):
            return True
    return False


def pytest_scope(tokens: list[str]) -> str:
    """'targeted' when a pytest call names paths or -k, otherwise 'full'."""
    if "pytest" not in tokens:
        return "full"
    skip_next = False
    for arg in tokens[tokens.index("pytest") + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-k"):
            return "targeted"
        if arg in VALUE_OPTIONS:
            skip_next = True
            continue
        if not arg.startswith("-") and ("/" in arg or arg.endswith(".py") or "::" in arg):
            return "targeted"
    return "full"


def classify(segment: str) -> str | None:
    """The kind of test run in one simple command, or None if it runs no tests."""
    if not TEST_COMMAND.search(IMPORT.sub("", segment)):
        return None
    tokens = split(segment)
    markers = marker_expressions(tokens)
    markers += [a or b for a, b in CALL_MARKERS.findall(segment)]
    if selects("live", markers) or LIVE_ENV.search(segment):
        return "live"
    if (
        selects("e2e", markers)
        or "tests/e2e" in segment
        or re.search(r"check\.sh\s+e2e\b", segment)
    ):
        return "e2e"
    if re.search(r"check\.sh\s+full\b", segment) or selects("integration", markers):
        return "full"
    if "pytest.main" in segment:
        return "full"
    if re.search(PYTEST_RUN, segment):
        return pytest_scope(tokens)
    return "targeted"


def runs(command: str) -> list[tuple[str, str]]:
    """(kind, repeat key) for every test run in a possibly compound command."""
    found = []
    for segment in SEPARATOR.split(command):
        kind = classify(segment)
        if kind is not None:
            found.append((kind, kind + ":" + " ".join(sorted(split(segment)))))
    if LIVE_ENV.search(command) and found:
        found = [("live", key) for _, key in found]
    return found


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


def load_tally(path: Path) -> dict[str, dict[str, int]] | None:
    """The session's tally; an empty one if none exists yet; None if it is unreadable."""
    if not path.exists():
        return {"counts": {}, "repeats": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for part in ("counts", "repeats"):
        table = data.get(part)
        if not isinstance(table, dict) or not all(isinstance(v, int) for v in table.values()):
            return None
    return {"counts": data["counts"], "repeats": data["repeats"]}


def verdict(
    planned: list[tuple[str, str]], tally: dict[str, dict[str, int]], limits: dict[str, int]
) -> str:
    counts, repeats = dict(tally["counts"]), dict(tally["repeats"])
    for kind, key in planned:
        if kind == "live" or limits[kind] <= 0:
            return BLOCK_LIVE
        if counts.get(kind, 0) >= limits[kind]:
            return (
                f"Blocked by the test budget: this session has used its {limits[kind]} "
                f"{LABEL[kind]} run(s). Push the branch and let GitHub run the rest, or stop "
                "and report what is failing in the pull request."
            )
        if repeats.get(key, 0) >= limits["repeat"]:
            return (
                f"Blocked by the test budget: this test command has run {repeats[key]} times this "
                "session. Change approach, or stop and report the failure in the pull request."
            )
        counts[kind] = counts.get(kind, 0) + 1
        repeats[key] = repeats.get(key, 0) + 1
    tally["counts"], tally["repeats"] = counts, repeats
    return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
        command = str(event.get("tool_input", {}).get("command", ""))
        if not TEST_COMMAND.search(command):
            return 0
        if LOOP.search(command):
            print(BLOCK_LOOP, file=sys.stderr)
            return 2
        planned = runs(command)
        if not planned:
            return 0
        project = os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd", "."))
        limits = load_limits(project)
        path = state_file(str(event.get("session_id", "")))
        tally = load_tally(path)
        if tally is None:
            print(BLOCK_TALLY, file=sys.stderr)
            return 2
        reason = verdict(planned, tally, limits)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        path.write_text(json.dumps(tally))
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
