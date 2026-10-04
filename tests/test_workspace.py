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


def test_undecodable_file_is_a_workspace_error(tmp_path: Path) -> None:
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    (copy / "tools.yaml").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkspaceError, match=r"tools\.yaml"):
        load_workspace(copy)


@pytest.mark.parametrize(
    ("principals", "contact", "problem"),
    [
        ("[{id: a@example.com, aliases: [X@example.com]}, {id: x@example.com}]",
         "boss@example.com", "x@example.com names both a@example.com and x@example.com"),
        ("[{id: a@example.com, aliases: [hr@example.com]}]",
         "HR@example.com", "escalation_contact hr@example.com is an alias; use a@example.com"),
        ("[{id: a@example.com, oid: 1f2e}, {id: b@example.com, oid: 1f2e}]",
         "boss@example.com", "1f2e names both a@example.com and b@example.com"),
    ],
    ids=["shared-name", "alias-contact", "shared-oid"],
)  # fmt: skip
def test_principal_names_must_be_unambiguous(
    tmp_path: Path, principals: str, contact: str, problem: str
) -> None:
    """Issue #19: an alias may not let one principal sign in or approve as another."""
    copy = tmp_path / "ws"
    shutil.copytree(EXAMPLE, copy)
    (copy / "principals.yaml").write_text(f"principals: {principals}\n")
    colleague = copy / "colleagues/harper.yaml"
    text = re.sub(
        r"escalation_contact: .*", f"escalation_contact: {contact}", colleague.read_text()
    )
    colleague.write_text(text)
    with pytest.raises(WorkspaceError, match=re.escape(problem)):
        load_workspace(copy)
