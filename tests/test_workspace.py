"""Workspace loader and `cograil validate` tests: one per acceptance criterion of issue #7."""

import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cograil.cli import app
from cograil.errors import WorkspaceError
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "workspaces/example-smb"
BROKEN = Path(__file__).parent / "fixtures/workspaces/broken-unknown-tool"

runner = CliRunner()


def test_example_workspace_loads() -> None:
    workspace = load_workspace(EXAMPLE)
    assert workspace.name == "example-smb"
    assert [c.name for c in workspace.colleagues] == ["harper"]
    assert {p.name for p in workspace.protocols} == {"leave_request", "policy_question"}
    assert "hris.submit_leave" in {t.name for t in workspace.tools}
    assert [a.name for a in workspace.audiences] == ["all-employees", "managers"]


@pytest.mark.parametrize(
    "missing",
    ["tools.yaml", "colleagues", "protocols"],
)
def test_missing_required_file_names_the_file(tmp_path: Path, missing: str) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    target = copy / missing
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    with pytest.raises(WorkspaceError, match=re.escape(str(target))):
        load_workspace(copy)


def test_optional_files_may_be_absent(tmp_path: Path) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    for name in ("connections.yaml", "audiences.yaml", "knowledge.yaml"):
        (copy / name).unlink()
    workspace = load_workspace(copy)
    assert workspace.connections == workspace.audiences == workspace.knowledge == []


def test_invalid_yaml_names_the_file(tmp_path: Path) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    (copy / "tools.yaml").write_text("tools: [unclosed")
    with pytest.raises(WorkspaceError, match=r"tools\.yaml"):
        load_workspace(copy)


def test_unknown_tool_fails_with_step_and_tool_name() -> None:
    with pytest.raises(WorkspaceError) as caught:
        load_workspace(BROKEN)
    message = str(caught.value)
    assert "leave_request.md" in message
    assert "@hris.submit_leave (step 2)" in message


def test_validate_exits_zero_on_example() -> None:
    result = runner.invoke(app, ["validate", str(EXAMPLE)])
    assert result.exit_code == 0
    assert "ok: example-smb" in result.output


def test_validate_exits_non_zero_on_broken_workspace() -> None:
    result = runner.invoke(app, ["validate", str(BROKEN)])
    assert result.exit_code == 1
    assert "@hris.submit_leave (step 2)" in result.output
