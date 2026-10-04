#!/usr/bin/env python3
"""Autopilot dispatcher: one pass that keeps one backlog task in flight (ADR 0015).

Runs on the owner's Mac every 30 minutes (launchd, installed by `autopilot install`).
Talks to GitHub through `gh` (ghcli.py) and starts cloud sessions through `claude`.
Standard library only, and Python 3.9 compatible so the macOS system `python3` runs it.

    dispatch.py              one pass
    dispatch.py --dry-run    one pass that prints every decision and changes nothing
    dispatch.py --status     state, next task, caps used today, estimated spend
    dispatch.py --balance X  post `balance $X` on the Credit Ledger
    dispatch.py --resume     clear the paused flag (`autopilot on` calls this)
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ghcli import LEDGER_TITLE, REPO, AutopilotError, GitHub, run

UTC = timezone.utc  # noqa: UP017 - datetime.UTC needs Python 3.11
PROMPT = "Follow .claude/commands/implement-issue.md for GitHub issue #{n} in {repo}."
LANES: dict[str, dict[str, Any]] = {
    "1": {"model": "claude-opus-5-5", "effort": "xhigh", "target": 11.0},
    "2": {"model": "claude-sonnet-5-5", "effort": "high", "target": 3.75},
}
LANE_LABELS = {"lane:1", "lane:2", "lane:3"}
SKIP_LABELS = {"needs:human", "type:epic"}
DAILY_SESSIONS = 6
SPEND_CAP = 225.0
CREDIT = 250.0
CHECK_FAILING_LIMIT = timedelta(hours=1)
UNMERGED_LIMIT = timedelta(hours=2)
NO_PR_LIMIT = timedelta(hours=3)
BALANCE = re.compile(r"\bbalance \$(\d+(?:\.\d+)?)", re.IGNORECASE)
SESSION_LINK = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
URL = re.compile(r"https://\S+")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fresh_state() -> dict[str, Any]:
    return {
        "in_flight": None,
        "day": "",
        "sessions_today": 0,
        "spend": 0.0,
        "balance_comment_id": 0,
        "paused": False,
        "paused_reason": "",
    }


@contextlib.contextmanager
def pass_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock for one pass; yields False when another pass holds it."""
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
        else:
            yield True


class Autopilot:
    def __init__(self, home: Path, now: datetime, dry_run: bool) -> None:
        self.home = home
        self.now = now
        self.dry_run = dry_run
        self.gh = GitHub(dry_run)
        self.state_path = home / "autopilot.json"
        self.state = fresh_state()
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text()))
        self.ledger = 0

    def decide(self, message: str) -> None:
        """One line per decision: printed on a dry run, appended to the log otherwise."""
        if self.dry_run:
            print(f"[dry-run] {message}")
            return
        with (self.home / "autopilot.log").open("a") as log:
            log.write(f"{stamp(datetime.now(UTC))} {message}\n")

    def save(self) -> None:
        if not self.dry_run:
            self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    def pause(self, reason: str) -> None:
        self.state.update(paused=True, paused_reason=reason)
        self.save()  # before the ledger note, so a failed note never lets the next pass launch
        self.decide(f"paused: {reason}")
        self.gh.comment(self.ledger, f"Autopilot paused: {reason}. `autopilot on` resumes.")

    def run_pass(self) -> int:
        if self.gh.switch() != "on":
            if self.dry_run:
                print("[dry-run] AUTOPILOT is not on: nothing to do")
            return 0
        try:
            if self.state["day"] != self.now.date().isoformat():
                self.state.update(day=self.now.date().isoformat(), sessions_today=0)
            self.ledger = self.gh.ledger()
            self.apply_balance()
            if self.state["paused"]:
                self.decide(f"paused: {self.state['paused_reason']}")
            elif self.state["in_flight"] is None or self.slot_free():
                self.start_next()
        finally:
            self.save()
        return 0

    def apply_balance(self) -> None:
        """The owner's latest new `balance $X` ledger comment resets spend to 250 - X."""
        latest: tuple[int, float] | None = None
        for comment in self.gh.comments(self.ledger):
            match = BALANCE.search(comment["body"] or "")
            fresh = comment["id"] > self.state["balance_comment_id"]
            if match and fresh and comment["assoc"] == "OWNER":
                latest = (int(comment["id"]), float(match.group(1)))
        if latest is not None:
            self.state["balance_comment_id"], balance = latest
            self.state["spend"] = round(CREDIT - balance, 2)
            self.decide(f"balance ${balance:.2f}: estimated spend ${self.state['spend']:.2f}")

    def next_task(self) -> tuple[int, str] | None:
        """The first ready task by milestone due date, then issue number: (issue, lane)."""
        candidates: list[tuple[str, int, str]] = []
        for issue in self.gh.ready_issues():
            names = {label["name"] for label in issue["labels"]}
            lanes = sorted(names & LANE_LABELS)
            if len(lanes) != 1 or names & SKIP_LABELS or issue["title"] == LEDGER_TITLE:
                continue
            due = (issue.get("milestone") or {}).get("dueOn") or "9999"
            candidates.append((due, int(issue["number"]), lanes[0][-1]))
        if not candidates:
            return None
        _, number, lane = min(candidates)
        return number, lane

    def slot_free(self) -> bool:
        """Settle the task in flight; True when it finished and the next may start."""
        task = self.state["in_flight"]
        issue = int(task["issue"])
        if self.gh.issue_state(issue) == "CLOSED":
            self.gh.comment(self.ledger, f"finished #{issue}")
            self.state["in_flight"] = None
            self.decide(f"#{issue} finished")
            return True
        branch = re.compile(rf"^claude/(?:github-)?issue-{issue}-")
        prs = [pr for pr in self.gh.open_prs() if branch.match(pr["headRefName"])]
        reason = self.pr_problem(prs[0]) if prs else self.no_pr_problem(task)
        if reason:
            self.escalate(issue, reason)
        else:
            waiting = f"PR #{prs[0]['number']}" if prs else "a pull request"
            self.decide(f"#{issue} in flight: waiting on {waiting}")
        return False

    def pr_problem(self, pr: dict[str, Any]) -> str:
        number = int(pr["number"])
        if "needs:human" in {label["name"] for label in pr["labels"]}:
            return f"PR #{number} is labelled needs:human"
        for check in self.gh.required_checks(number):
            done = check.get("completedAt") or ""
            if check["bucket"] == "fail" and done and self.over(done, CHECK_FAILING_LIMIT):
                return f"required check {check['name']} failing for over an hour on PR #{number}"
        if self.over(pr["createdAt"], UNMERGED_LIMIT):
            return f"PR #{number} still unmerged two hours after it opened"
        return ""

    def no_pr_problem(self, task: dict[str, Any]) -> str:
        if self.over(task["started"], NO_PR_LIMIT):
            return "no pull request three hours after launch"
        return ""

    def over(self, since: str, limit: timedelta) -> bool:
        return self.now - parse_time(since) > limit

    def escalate(self, issue: int, reason: str) -> None:
        self.gh.label(issue, "--add-label needs:human")
        self.state["in_flight"] = None
        self.pause(f"#{issue} needs a human: {reason}")

    def start_next(self) -> None:
        task = self.next_task()
        if task is None:
            self.decide("no ready task")
            return
        issue, lane = task
        if lane == "3":
            found = URL.search(self.gh.run_lane3(issue))
            link = found.group(0) if found else "lane3-implement.yml"
            self.started(issue, "3", "GLM via lane3-implement.yml", 0.0, link)
            return
        target = LANES[lane]["target"]
        if self.state["sessions_today"] >= DAILY_SESSIONS:
            self.decide(f"#{issue} waits: {DAILY_SESSIONS} cloud sessions already started today")
        elif self.state["spend"] + target > SPEND_CAP:
            spend = self.state["spend"]
            self.decide(f"#{issue} waits: ${spend:.2f} + ${target:.2f} would pass ${SPEND_CAP:.0f}")
        else:
            self.launch_cloud(issue, lane)

    def claude_command(self, issue: int, lane: str) -> list[str]:
        """Use only the options `claude --help` lists: --cloud or --remote, model, effort."""
        proc = run(["claude", "--help"])
        options = set(re.findall(r"--[a-z][a-z-]*", proc.stdout + proc.stderr))
        cmd = ["claude"]
        if "--model" in options:
            cmd += ["--model", LANES[lane]["model"]]
        if "--effort" in options:
            cmd += ["--effort", LANES[lane]["effort"]]
        environment = os.environ.get("AUTOPILOT_ENVIRONMENT", "")
        if environment and "--environment" in options:
            cmd += ["--environment", environment]
        cmd.append("--cloud" if "--cloud" in options else "--remote")
        return [*cmd, PROMPT.format(n=issue, repo=REPO)]

    def launch_cloud(self, issue: int, lane: str) -> None:
        cmd = self.claude_command(issue, lane)
        if self.dry_run:
            print(f"[dry-run] would run: {' '.join(cmd)}")
            link = "(dry run)"
        else:
            try:
                proc = run(cmd, timeout=600)
            except AutopilotError as exc:  # the session may exist: pause rather than relaunch
                self.pause(f"launching #{issue} failed: {exc}")
                return
            output = proc.stdout + proc.stderr
            self.decide(f"#{issue} claude output: {json.dumps(output)}")
            if proc.returncode != 0:
                self.pause(f"claude exited {proc.returncode} launching #{issue}")
                return
            found = SESSION_LINK.search(output)
            link = found.group(0) if found else "(no session link in claude output)"
        cfg = LANES[lane]
        self.state["sessions_today"] += 1
        self.state["spend"] = round(self.state["spend"] + cfg["target"], 2)
        self.started(issue, lane, f"{cfg['model']} {cfg['effort']}", cfg["target"], link)

    def started(self, issue: int, lane: str, model: str, estimate: float, link: str) -> None:
        flight = {"issue": issue, "lane": int(lane), "started": stamp(self.now), "session": link}
        self.state["in_flight"] = flight
        self.save()  # before any further GitHub call, so a failure never relaunches
        self.gh.label(issue, "--add-label status:in-progress --remove-label status:ready")
        when = self.now.strftime("%Y-%m-%d %H:%M UTC")
        line = f"#{issue} | lane {lane} | {model} | started {when} | est ${estimate:.2f} | {link}"
        self.gh.comment(self.ledger, line)
        self.decide(f"#{issue} started on lane {lane}: {link}")

    def status(self) -> int:
        today = self.now.date().isoformat()
        used = self.state["sessions_today"] if self.state["day"] == today else 0
        task, nxt = self.state["in_flight"], self.next_task()
        flight = f"#{task['issue']} lane {task['lane']} since {task['started']}" if task else ""
        print(f"switch:     AUTOPILOT={self.gh.switch() or 'unset'}")
        print(f"paused:     {self.state['paused_reason'] if self.state['paused'] else 'no'}")
        print(f"in flight:  {flight or 'none'} {task['session'] if task else ''}".rstrip())
        print(f"next task:  {f'#{nxt[0]} lane {nxt[1]}' if nxt else 'none'}")
        print(f"today:      {used} of {DAILY_SESSIONS} cloud sessions")
        print(f"est spend:  ${self.state['spend']:.2f} of ${SPEND_CAP:.0f} cap")
        return 0

    def post_balance(self, amount: float) -> int:
        self.ledger = self.gh.ledger()
        self.gh.comment(self.ledger, f"balance ${amount:.2f}")
        print(f"posted balance ${amount:.2f} on #{self.ledger}")
        return 0

    def resume(self) -> int:
        self.state.update(paused=False, paused_reason="")
        self.save()
        return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autopilot dispatcher (ADR 0015).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="print decisions, change nothing")
    group.add_argument("--status", action="store_true", help="show state and caps")
    group.add_argument("--balance", type=float, metavar="AMOUNT", help="post the credit balance")
    group.add_argument("--resume", action="store_true", help="clear the paused flag")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    args = parse_args(argv)
    home = Path.home() / ".cograil"
    if not args.dry_run:
        home.mkdir(parents=True, exist_ok=True)
    pilot = Autopilot(home, now or datetime.now(UTC), args.dry_run)
    try:
        if args.status:
            return pilot.status()
        if args.balance is not None:
            return pilot.post_balance(args.balance)
        if args.resume:
            return pilot.resume()
        if args.dry_run:
            return pilot.run_pass()
        with pass_lock(home / "autopilot.lock") as acquired:
            if acquired:
                return pilot.run_pass()
            pilot.decide("another pass holds the lock: skipped")
            return 0
    except AutopilotError as exc:
        pilot.decide(f"error: {exc}")
        print(f"autopilot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
