#!/usr/bin/env python3
"""Create the Cograil backlog on GitHub from scripts/backlog.json.

Usage:
    python scripts/create_backlog.py --repo cognitive-builder/cograil [--dry-run]

Requires the GitHub CLI (`gh auth login` with repo scope). Idempotent for labels
and milestones; issues are created once, so run it against an empty issue list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

BACKLOG = Path(__file__).with_name("backlog.json")


def gh(args: list[str], dry_run: bool) -> str:
    cmd = ["gh", *args]
    if dry_run:
        shown = cmd if len(" ".join(cmd)) < 200 else [*cmd[:6], "..."]
        print("DRY  " + " ".join(shown))
        return ""
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def ensure_labels(repo: str, labels: list[dict], dry_run: bool) -> None:
    for label in labels:
        gh(
            [
                "label",
                "create",
                label["name"],
                "--repo",
                repo,
                "--color",
                label["color"],
                "--description",
                label["description"],
                "--force",
            ],
            dry_run,
        )


def ensure_milestones(repo: str, milestones: list[dict], dry_run: bool) -> None:
    existing = set()
    if not dry_run:
        raw = gh(["api", f"repos/{repo}/milestones?state=all&per_page=100"], dry_run)
        existing = {m["title"] for m in json.loads(raw or "[]")}
    for m in milestones:
        if m["title"] in existing:
            continue
        gh(
            [
                "api",
                f"repos/{repo}/milestones",
                "-f",
                f"title={m['title']}",
                "-f",
                f"due_on={m['due']}T23:59:59Z",
                "-f",
                f"description={m['description']}",
            ],
            dry_run,
        )


def create_issue(
    repo: str, title: str, body: str, labels: list[str], milestone: str, dry_run: bool
) -> int:
    args = [
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--milestone",
        milestone,
    ]
    for label in labels:
        args += ["--label", label]
    url = gh(args, dry_run)
    if dry_run:
        return 0
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        raise RuntimeError(f"could not parse issue number from: {url}")
    return int(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = json.loads(BACKLOG.read_text())
    ensure_labels(args.repo, data["labels"], args.dry_run)
    ensure_milestones(args.repo, data["milestones"], args.dry_run)

    milestone_by_phase = {e["phase"]: e["milestone"] for e in data["epics"]}
    epic_numbers: dict[int, int] = {}
    for epic in data["epics"]:
        number = create_issue(
            args.repo,
            epic["title"],
            epic["body"],
            [f"phase:{epic['phase']}", "type:epic"],
            epic["milestone"],
            args.dry_run,
        )
        epic_numbers[epic["phase"]] = number

    children: dict[int, list[tuple[int, str]]] = {p: [] for p in milestone_by_phase}
    for issue in data["issues"]:
        labels = [
            f"phase:{issue['phase']}",
            f"type:{issue['type']}",
            f"size:{issue['size']}",
            f"lane:{issue['lane']}",
            "status:ready",
        ]
        if issue.get("e2e"):
            labels.append("needs:e2e")
        epic_ref = "" if args.dry_run else f"\n\nEpic: #{epic_numbers[issue['phase']]}"
        number = create_issue(
            args.repo,
            issue["title"],
            issue["body"] + epic_ref,
            labels,
            milestone_by_phase[issue["phase"]],
            args.dry_run,
        )
        children[issue["phase"]].append((number, issue["title"]))
        print(f"created #{number or issue['id']}: {issue['title']}")

    if args.dry_run:
        print(
            f"\nDry run: {len(data['epics'])} epics, {len(data['issues'])} issues, "
            f"{len(data['labels'])} labels, {len(data['milestones'])} milestones."
        )
        return 0

    for phase, epic_number in epic_numbers.items():
        epic = next(e for e in data["epics"] if e["phase"] == phase)
        task_list = "\n".join(f"- [ ] #{n} {t}" for n, t in children[phase])
        gh(
            [
                "issue",
                "edit",
                str(epic_number),
                "--repo",
                args.repo,
                "--body",
                epic["body"] + "\n\n## Children\n\n" + task_list,
            ],
            False,
        )
        print(f"linked {len(children[phase])} children to epic #{epic_number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
