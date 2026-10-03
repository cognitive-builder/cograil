"""The test-budget guard (ADR 0014): limits hold, real-model runs never pass, and
a session cannot loosen its own budget by editing the working copy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "test_budget.py"


def run_hook(command: str, tmp_path: Path, budget: dict[str, int] | None = None) -> int:
    env = {"COGRAIL_TEST_BUDGET_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if budget is not None:
        limits = tmp_path / "budget.json"
        limits.write_text(json.dumps(budget))
        env["COGRAIL_TEST_BUDGET_FILE"] = str(limits)
    event = {"session_id": "s1", "tool_input": {"command": command}, "cwd": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event), text=True, env=env, check=False
    )
    return result.returncode


@pytest.mark.parametrize("command", ["ls -la", "git status", "uv run ruff check ."])
def test_non_test_commands_pass(command: str, tmp_path: Path) -> None:
    assert run_hook(command, tmp_path) == 0


@pytest.mark.parametrize(
    "command", ["uv run pytest -m live", "COGRAIL_LIVE=1 uv run cograil eval ws"]
)
def test_real_model_runs_are_always_blocked(command: str, tmp_path: Path) -> None:
    assert run_hook(command, tmp_path) == 2


def test_full_suite_limit(tmp_path: Path) -> None:
    budget = {"targeted": 20, "repeat": 5, "full": 2, "e2e": 1, "live": 0}
    results = [run_hook(f"uv run pytest --tb {s}", tmp_path, budget) for s in ("a", "b", "c")]
    assert results == [0, 0, 2]


def test_repeat_limit(tmp_path: Path) -> None:
    budget = {"targeted": 20, "repeat": 3, "full": 2, "e2e": 1, "live": 0}
    results = [run_hook("uv run pytest tests/test_a.py", tmp_path, budget) for _ in range(4)]
    assert results == [0, 0, 0, 2]


def test_end_to_end_runs_once(tmp_path: Path) -> None:
    assert [run_hook("scripts/check.sh e2e", tmp_path) for _ in range(2)] == [0, 2]


def test_limits_come_from_main_not_the_working_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "test-budget.json").write_text(json.dumps({"e2e": 0}))
    subprocess.run([*git, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "budget"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    (repo / ".claude" / "test-budget.json").write_text(json.dumps({"e2e": 99}))
    event = {"session_id": "s2", "tool_input": {"command": "scripts/check.sh e2e"}}
    env = {"COGRAIL_TEST_BUDGET_STATE_DIR": str(tmp_path), "CLAUDE_PROJECT_DIR": str(repo)}
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event), text=True, env=env, check=False
    )
    assert result.returncode == 2


def test_broken_input_never_blocks(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(HOOK)], input="not json", text=True, check=False)
    assert result.returncode == 0
