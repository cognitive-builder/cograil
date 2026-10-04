"""Turn an issue form's Lane and Testing answers into labels (used by lane-label.yml).

Reads the issue body on stdin and the issue's current labels as a JSON list in
CURRENT_LABELS. Prints two lines: labels to add, then labels to remove, each
comma-separated (empty when there is nothing to do).
"""

from __future__ import annotations

import json
import os
import re
import sys

LANES = {"lane:1", "lane:2", "lane:3"}


def answer(body: str, question: str) -> str:
    """The response under an issue form heading, or '' if absent."""
    match = re.search(rf"^###\s+{re.escape(question)}\s*\n+(.+?)\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else ""


def labels_for(body: str, current: list[str]) -> tuple[list[str], list[str]]:
    lane = re.match(r"Lane ([123])\b", answer(body, "Lane"))
    want = {f"lane:{lane.group(1)}"} if lane else set()
    if answer(body, "Testing needed").startswith("End-to-end required"):
        want.add("needs:e2e")
    add = sorted(want - set(current))
    remove = sorted((set(current) & LANES) - want) if lane else []
    return add, remove


def main() -> int:
    current = json.loads(os.environ.get("CURRENT_LABELS") or "[]")
    add, remove = labels_for(sys.stdin.read(), current)
    print(",".join(add))
    print(",".join(remove))
    return 0


if __name__ == "__main__":
    sys.exit(main())
