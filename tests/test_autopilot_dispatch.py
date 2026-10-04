"""Autopilot dispatcher decisions with stub `gh` and `claude` on PATH (issue #61, ADR 0015)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/autopilot"))  # dispatch.py imports its sibling ghcli.py
SPEC = importlib.util.spec_from_file_location("dispatch", ROOT / "scripts/autopilot/dispatch.py")
assert SPEC is not None and SPEC.loader is not None
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
LEDGER = "Credit Ledger (not a task)"
CLOUD_HELP = "Usage: claude [options]\n  --model <m>\n  --effort <e>\n  --cloud <prompt>\n"
LINK = "https://claude.ai/code/session_01abc"

# Matches each call's arguments against rule prefixes in order; logs every call.
STUB = f"""#!{sys.executable}
import json, os, sys
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
with open(os.environ["STUB_RULES"]) as f:
    rules = json.load(f).get(name, [])
for prefix, out, rc in rules:
    if " ".join(args).startswith(prefix):
        sys.stdout.write(out)
        sys.exit(rc)
"""


def ago(**delta: float) -> str:
    return dispatch.stamp(NOW - timedelta(**delta))


def issue(number: int, lane: str = "lane:2", due: str | None = None, *extra: str) -> dict[str, Any]:
    labels = [{"name": name} for name in ("status:ready", lane, *extra)]
    milestone = {"title": "m", "dueOn": due} if due else None
    return {"number": number, "title": f"task {number}", "labels": labels, "milestone": milestone}


class World:
    """A fake HOME plus stub executables whose answers each test sets."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home, self.log, self.rules = tmp / "home", tmp / "calls.jsonl", tmp / "rules.json"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name in ("gh", "claude"):
            (bin_dir / name).write_text(STUB)
            (bin_dir / name).chmod(0o755)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv("STUB_LOG", str(self.log))
        monkeypatch.setenv("STUB_RULES", str(self.rules))
        monkeypatch.delenv("AUTOPILOT_ENVIRONMENT", raising=False)
        self.setup()

    def setup(
        self,
        *,
        switch: str = "on",
        issues: tuple[dict[str, Any], ...] = (),
        state: str = "OPEN",
        prs: tuple[dict[str, Any], ...] = (),
        checks: tuple[dict[str, Any], ...] = (),
        comments: tuple[dict[str, Any], ...] = (),
        help_text: str = CLOUD_HELP,
        launch_rc: int = 0,
    ) -> None:
        gh = [
            ["variable get AUTOPILOT", switch + "\n", 0],
            ["issue list --state open --json", json.dumps([{"number": 1, "title": LEDGER}]), 0],
            ["issue list --state open --label status:ready", json.dumps(list(issues)), 0],
            ["issue view", json.dumps({"state": state}), 0],
            ["pr list", json.dumps(list(prs)), 0],
            ["pr checks", json.dumps(list(checks)), 1 if checks else 0],
            ["api --paginate", "".join(json.dumps(c) + "\n" for c in comments), 0],
        ]
        claude = [["--help", help_text, 0], ["", f"Started {LINK}\n", launch_rc]]
        self.rules.write_text(json.dumps({"gh": gh, "claude": claude}))

    def write_state(self, **values: Any) -> None:
        self.home.joinpath(".cograil").mkdir(parents=True, exist_ok=True)
        state = {**dispatch.fresh_state(), "day": NOW.date().isoformat(), **values}
        self.home.joinpath(".cograil/autopilot.json").write_text(json.dumps(state))

    def state(self) -> dict[str, Any]:
        return dict(json.loads(self.home.joinpath(".cograil/autopilot.json").read_text()))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def launches(self) -> list[list[str]]:
        return [c for c in self.calls() if c[0] == "claude" and c[1:] != ["--help"]]

    def gh_writes(self) -> list[list[str]]:
        writes = ("issue edit", "issue comment", "workflow run", "variable set", "api -X")
        return [c for c in self.calls() if c[0] == "gh" and " ".join(c[1:]).startswith(writes)]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def run(*argv: str) -> int:
    return int(dispatch.main(list(argv), now=NOW))


def in_flight(started: str | None = None) -> dict[str, Any]:
    return {"issue": 5, "lane": 2, "started": started or ago(hours=1), "session": LINK}


def test_picks_next_task_by_due_date_then_number(world: World) -> None:
    world.setup(
        issues=(
            issue(3),
            issue(9, "lane:2", "2026-10-09T00:00:00Z"),
            issue(8, "lane:2", "2026-10-05T00:00:00Z"),
            issue(7, "lane:2", "2026-10-05T00:00:00Z"),
            issue(2, "lane:2", "2026-10-01T00:00:00Z", "needs:human"),
            issue(4, "lane:2", "2026-10-01T00:00:00Z", "type:epic"),
            issue(6, "lane:2", "2026-10-01T00:00:00Z", "lane:3"),
            {**issue(10, "lane:2", "2026-10-01T00:00:00Z"), "title": LEDGER},
        )
    )
    assert run() == 0
    [launch] = world.launches()
    prompt = "Follow .claude/commands/implement-issue.md for GitHub issue #7 in {}."
    assert launch[-1] == prompt.format("cognitive-builder/cograil")
    assert world.state()["in_flight"]["issue"] == 7
    assert [
        "gh",
        "issue",
        "edit",
        "7",
        "--add-label",
        "status:in-progress",
        "--remove-label",
        "status:ready",
        "-R",
        "cognitive-builder/cograil",
    ] in world.gh_writes()
    ledger = [c for c in world.gh_writes() if c[1:3] == ["issue", "comment"]]
    assert ledger[0][5] == (
        f"#7 | lane 2 | claude-sonnet-5-5 high | started 2026-10-04 12:00 UTC | est $3.75 | {LINK}"
    )


def test_one_task_in_flight_waits_for_its_pull_request(world: World) -> None:
    pr = {
        "number": 40,
        "headRefName": "claude/issue-5-x",
        "labels": [],
        "createdAt": ago(hours=1),
        "url": "u",
    }
    world.setup(issues=(issue(9),), prs=(pr,))
    world.write_state(in_flight=in_flight())
    run()
    assert world.launches() == [] and world.gh_writes() == []
    assert world.state()["in_flight"]["issue"] == 5


def test_closed_task_is_finished_and_the_next_starts(world: World) -> None:
    world.setup(issues=(issue(9),), state="CLOSED")
    world.write_state(in_flight=in_flight())
    run()
    assert [
        "gh",
        "issue",
        "comment",
        "1",
        "--body",
        "finished #5",
        "-R",
        "cognitive-builder/cograil",
    ] in world.gh_writes()
    assert world.state()["in_flight"]["issue"] == 9


def test_lane3_runs_the_workflow_without_a_credit_estimate(world: World) -> None:
    world.setup(issues=(issue(9, "lane:3"),))
    run()
    assert world.launches() == []
    assert [
        "gh",
        "workflow",
        "run",
        "lane3-implement.yml",
        "-f",
        "issue=9",
        "-R",
        "cognitive-builder/cograil",
    ] in world.gh_writes()
    assert world.state()["spend"] == 0.0 and world.state()["sessions_today"] == 0


@pytest.mark.parametrize(
    ("day", "used", "launched"),
    [
        ("2026-10-04", 6, False),
        ("2026-10-04", 5, True),
        ("2026-10-03", 6, True),
    ],
)
def test_daily_cap_of_six_cloud_sessions(world: World, day: str, used: int, launched: bool) -> None:
    world.setup(issues=(issue(9),))
    world.write_state(day=day, sessions_today=used)
    run()
    assert bool(world.launches()) is launched
    expected = used if day == "2026-10-04" else 0
    assert world.state()["sessions_today"] == expected + int(launched)


@pytest.mark.parametrize(
    ("lane", "spend", "launched"),
    [
        ("lane:1", 214.0, True),
        ("lane:1", 214.01, False),
        ("lane:2", 221.25, True),
        ("lane:2", 221.26, False),
    ],
)
def test_spend_cap(world: World, lane: str, spend: float, launched: bool) -> None:
    world.setup(issues=(issue(9, lane),))
    world.write_state(spend=spend)
    run()
    assert bool(world.launches()) is launched


def test_balance_comment_recalibrates_spend(world: World) -> None:
    world.setup(
        comments=(
            {"id": 10, "body": "balance $50", "assoc": "OWNER"},
            {"id": 11, "body": "balance $10", "assoc": "NONE"},
            {"id": 12, "body": "Owner note: balance $100.50 today", "assoc": "OWNER"},
        )
    )
    world.write_state(spend=3.0, balance_comment_id=9)
    run()
    assert world.state()["spend"] == 149.5 and world.state()["balance_comment_id"] == 12
    world.write_state(spend=160.0, balance_comment_id=12)
    run()
    assert world.state()["spend"] == 160.0


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        (CLOUD_HELP, ["--model", "claude-opus-5-5", "--effort", "xhigh", "--cloud"]),
        ("Usage: claude\n  --remote <prompt>  start a web session\n", ["--remote"]),
        ("  --model <m>\n  --remote\n  --cloud-sync\n", ["--model", "claude-opus-5-5", "--remote"]),
    ],
)
def test_cloud_or_remote_and_options_from_claude_help(
    world: World, help_text: str, expected: list[str]
) -> None:
    world.setup(issues=(issue(9, "lane:1"),), help_text=help_text)
    run()
    [launch] = world.launches()
    assert launch[1:-1] == expected


@pytest.mark.parametrize(
    ("prs", "checks", "started"),
    [
        ((), (), ago(hours=3, minutes=1)),
        (
            (
                {
                    "number": 40,
                    "headRefName": "claude/github-issue-5-ab",
                    "labels": [],
                    "createdAt": ago(hours=2, minutes=1),
                    "url": "u",
                },
            ),
            (),
            ago(hours=2, minutes=30),
        ),
        (
            (
                {
                    "number": 40,
                    "headRefName": "claude/issue-5-x",
                    "labels": [{"name": "needs:human"}],
                    "createdAt": ago(minutes=10),
                    "url": "u",
                },
            ),
            (),
            ago(minutes=30),
        ),
        (
            (
                {
                    "number": 40,
                    "headRefName": "claude/issue-5-x",
                    "labels": [],
                    "createdAt": ago(minutes=90),
                    "url": "u",
                },
            ),
            ({"name": "ci", "bucket": "fail", "completedAt": ago(minutes=61)},),
            ago(minutes=100),
        ),
    ],
    ids=["no-pr-after-3h", "unmerged-after-2h", "pr-labelled", "check-failing-1h"],
)
def test_needs_human_escalation(
    world: World, prs: tuple[dict[str, Any], ...], checks: tuple[dict[str, Any], ...], started: str
) -> None:
    world.setup(issues=(issue(9),), prs=prs, checks=checks)
    world.write_state(in_flight=in_flight(started))
    run()
    assert [
        "gh",
        "issue",
        "edit",
        "5",
        "--add-label",
        "needs:human",
        "-R",
        "cognitive-builder/cograil",
    ] in world.gh_writes()
    state = world.state()
    assert state["paused"] and state["in_flight"] is None
    assert world.launches() == []
    run()
    assert world.launches() == []


def test_failed_launch_pauses_without_counting(world: World) -> None:
    world.setup(issues=(issue(9),), launch_rc=1)
    run()
    state = world.state()
    assert state["paused"] and state["in_flight"] is None and state["sessions_today"] == 0
    assert not [c for c in world.gh_writes() if c[1:3] == ["issue", "edit"]]


@pytest.mark.parametrize(
    ("prs", "started"),
    [
        ((), ago(hours=2, minutes=59)),
        (
            (
                {
                    "number": 40,
                    "headRefName": "claude/issue-56-x",
                    "labels": [],
                    "createdAt": ago(hours=5),
                    "url": "u",
                },
            ),
            ago(hours=2),
        ),
    ],
)
def test_no_escalation_before_the_limits(
    world: World, prs: tuple[dict[str, Any], ...], started: str
) -> None:
    world.setup(prs=prs)
    world.write_state(in_flight=in_flight(started))
    run()
    assert world.gh_writes() == [] and not world.state()["paused"]


def test_off_switch_exits_quietly(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.setup(switch="off", issues=(issue(9),))
    assert run() == 0
    assert [c[1:3] for c in world.calls()] == [["variable", "get"]]
    assert not world.home.joinpath(".cograil/autopilot.json").exists()
    assert capsys.readouterr().out == ""


def test_dry_run_has_no_side_effects(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.setup(issues=(issue(9, "lane:1"),), state="CLOSED")
    world.write_state(in_flight=in_flight())
    before = world.home.joinpath(".cograil/autopilot.json").read_text()
    assert run("--dry-run") == 0
    assert world.launches() == [] and world.gh_writes() == []
    assert world.home.joinpath(".cograil/autopilot.json").read_text() == before
    assert not world.home.joinpath(".cograil/autopilot.log").exists()
    out = capsys.readouterr().out
    assert "#5 finished" in out and "would run: claude" in out and "#9 started on lane 1" in out
