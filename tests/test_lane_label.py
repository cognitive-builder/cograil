"""The task form's Lane and Testing answers become labels (CodeRabbit finding on #53)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "lane_label", Path(__file__).resolve().parents[1] / "scripts" / "lane_label.py"
)
assert SPEC is not None and SPEC.loader is not None
lane_label = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lane_label)

BODY = """### Goal

Something useful.

### Lane

{lane}

### Testing needed

{testing}
"""


@pytest.mark.parametrize(
    ("lane", "testing", "current", "add", "remove"),
    [
        (
            "Lane 1: Opus 5.5, Ultracode (runner)",
            "Tests for the touched modules (default)",
            [],
            ["lane:1"],
            [],
        ),
        (
            "Lane 2: Sonnet 5.5, high (standard feature)",
            "End-to-end required",
            [],
            ["lane:2", "needs:e2e"],
            [],
        ),
        (
            "Lane 3: GLM via @claude on GitHub",
            "No tests (docs or config only)",
            ["lane:2"],
            ["lane:3"],
            ["lane:2"],
        ),
        ("Lane 2: Sonnet 5.5, high (standard feature)", "_No response_", ["lane:2"], [], []),
    ],
)
def test_form_answers_become_labels(
    lane: str, testing: str, current: list[str], add: list[str], remove: list[str]
) -> None:
    assert lane_label.labels_for(BODY.format(lane=lane, testing=testing), current) == (add, remove)


def test_issue_without_a_lane_question_is_left_alone() -> None:
    assert lane_label.labels_for("### Goal\n\nNo lane here.\n", ["lane:2"]) == ([], [])
