"""The autopilot workflows parse and carry the commands issue #61 names (ADR 0015)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("name", "needles"),
    [
        ("auto-merge.yml", ["gh pr merge --auto --squash", "gh pr merge --disable-auto"]),
        ("lane3-implement.yml", ["anthropics/claude-code-action@v1", "--max-turns 70"]),
    ],
)
def test_workflow_yaml_parses(name: str, needles: list[str]) -> None:
    text = (ROOT / ".github/workflows" / name).read_text()
    workflow = yaml.safe_load(text)
    assert workflow["jobs"] and workflow[True]  # PyYAML reads the `on:` key as True
    assert all(needle in text for needle in needles)
