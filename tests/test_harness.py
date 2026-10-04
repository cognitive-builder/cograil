"""Harness tests for issue #44: harness.yaml loading and the harness version."""

import shutil
from pathlib import Path

import pytest

from cograil.domain import Harness
from cograil.errors import WorkspaceError
from cograil.harness import harness_version
from cograil.workspace import load_workspace

EXAMPLE = Path(__file__).parents[1] / "workspaces/example-smb"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLE, tmp_path / "example-smb"))


def test_harness_yaml_is_loaded_per_workspace() -> None:
    harness = load_workspace(EXAMPLE).harness
    assert (harness.loop.max_turns, harness.loop.token_budget_per_step) == (6, 12000)
    assert harness.loop.usd_budget_per_run == 0.50
    assert (harness.tiers.small, harness.tiers.standard) == (
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
    )
    assert harness.context.compression_threshold_tokens == 2000
    assert (harness.retry.tool_attempts, harness.retry.backoff_seconds) == (2, 1)
    assert harness.approvals.timeout_hours == 72
    assert harness.pricing["claude-opus-5-5"].output_per_mtok == 20.00


def test_a_workspace_without_harness_yaml_gets_the_defaults(workspace: Path) -> None:
    (workspace / "harness.yaml").unlink()
    assert load_workspace(workspace).harness == Harness()


def test_the_version_changes_when_the_file_changes(workspace: Path) -> None:
    file = workspace / "harness.yaml"
    text = file.read_text()
    first = harness_version(load_workspace(workspace).harness)
    assert first.startswith("1.0.0+")
    file.write_text(text.replace("max_turns: 6", "max_turns: 7"))
    second = harness_version(load_workspace(workspace).harness)
    assert second.startswith("1.0.0+") and second != first
    file.write_text(text.replace("version: 1.0.0", "version: 1.1.0"))
    assert harness_version(load_workspace(workspace).harness).startswith("1.1.0+")
    file.write_text(f"# a comment cannot change behaviour\n{text}")
    assert harness_version(load_workspace(workspace).harness) == first


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("version: 1.0.0", "version: 1", id="not-semver"),
        pytest.param("  claude-opus-5-5: {", "  claude-opus-4-8: {", id="unpriced-tier-model"),
        pytest.param("  max_turns: 6", "  max_turns: 0", id="max-turns-below-one"),
        pytest.param("loop:", "loops:", id="unknown-key"),
    ],
)
def test_an_invalid_harness_yaml_is_a_workspace_error(workspace: Path, old: str, new: str) -> None:
    file = workspace / "harness.yaml"
    file.write_text(file.read_text().replace(old, new, 1))
    with pytest.raises(WorkspaceError, match=r"harness\.yaml"):
        load_workspace(workspace)
