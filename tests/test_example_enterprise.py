"""The example-enterprise workspace: one test per acceptance criterion of issue #34."""

import re
from pathlib import Path

import pytest
import yaml

from cograil.api.auth_settings import OidcAuth, auth_settings
from cograil.domain import Workspace
from cograil.knowledge.acl import groups_for
from cograil.registry import build_registry
from cograil.store import InMemoryRunStore
from cograil.workspace import load_workspace

ROOT = Path(__file__).parents[1]
PACK = ROOT / "workspaces/example-enterprise"
PLACEHOLDER = re.compile(r"<[a-z0-9-]+>|^$")


@pytest.fixture(scope="module")
def workspace() -> Workspace:
    return load_workspace(PACK)


def _env_example() -> dict[str, str]:
    lines = (PACK / "sso.env.example").read_text().splitlines()
    pairs = [line.split("=", 1) for line in lines if line and not line.startswith("#")]
    return {name: value for name, value in pairs}


def test_three_colleagues_each_with_a_protocol(workspace: Workspace) -> None:
    assert [c.name for c in workspace.colleagues] == ["finn", "hazel", "ivy"]
    protocols = {p.name for p in workspace.protocols}
    assert all(set(c.protocols) <= protocols for c in workspace.colleagues)
    audiences = {a.name for a in workspace.audiences}
    assert all(set(c.audiences) <= audiences for c in workspace.colleagues)


def test_sso_config_is_a_real_oidc_setting_with_placeholders_only() -> None:
    env = _env_example()
    assert env["COGRAIL_AUTH"] == "oidc"
    # Fill the placeholders with a stand-in value to prove the names are the ones the service reads.
    filled = {k: ("s" * 32 if PLACEHOLDER.search(v) else v) for k, v in env.items()}
    assert isinstance(auth_settings(filled), OidcAuth)


def test_entra_groups_and_oids_are_wired(workspace: Workspace) -> None:
    assert all(a.claims for a in workspace.audiences)
    assert all(p.oid for p in workspace.principals)


def test_sharepoint_style_folders_narrow_access_by_library() -> None:
    sites = {
        s["name"]: s for s in yaml.safe_load((PACK / "knowledge.yaml").read_text())["knowledge"]
    }
    finance = PACK / sites["finance-site"]["path"]
    it_site = PACK / sites["it-services-site"]["path"]
    expenses = finance / "Shared-Documents/Policies/expenses.md"
    vpn = it_site / "Shared-Documents/Runbooks/vpn-access.md"
    assert groups_for(expenses, finance, sites["finance-site"]["acl_groups"]) == ["finance"]
    assert groups_for(vpn, it_site, sites["it-services-site"]["acl_groups"]) == ["all-employees"]


def test_jira_tools_use_the_rest_kind_and_every_write_is_gated(workspace: Workspace) -> None:
    tools = {t.name: t for t in workspace.tools}
    jira = [t for name, t in tools.items() if name.startswith("jira.")]
    assert jira and all(t.kind == "rest" and t.connection == "jira" for t in jira)
    writes = [t for t in tools.values() if t.scope == "write"]
    assert {t.name for t in writes} == {"hris.submit_leave", "jira.create_issue", "notify.send"}
    assert all(t.confirm_before_write for t in writes)  # issue comment: a real notify is gated


def test_connections_reference_environment_variables_only(workspace: Workspace) -> None:
    for connection in workspace.connections:
        assert connection.secret_env
        assert all(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in connection.secret_env.values()
        )
    declared = _env_example().keys()
    assert {n for c in workspace.connections for n in c.secret_env.values()} <= declared


def test_no_file_holds_a_credential_value() -> None:
    # A secret-like key may hold a <placeholder> or the NAME of an environment variable.
    value = r"(?!<)(?![A-Z][A-Z0-9_]*$)\S{8,}"
    secretish = re.compile(rf"(secret|token|password|api[_-]?key)\s*[:=]\s*{value}", re.I)
    for path in PACK.rglob("*"):
        if path.is_file() and path.suffix in {".yaml", ".md", ".example"}:
            for line in path.read_text().splitlines():
                if secretish.search(line.strip()):
                    pytest.fail(f"{path}: looks like a credential: {line!r}")


async def test_registry_builds_over_the_pack(workspace: Workspace, store: InMemoryRunStore) -> None:
    async with await build_registry(workspace, store, PACK) as registry:
        names = {tool.name for tool in registry.tools}
    assert {"jira.create_issue", "jira.search_issues", "notify.send"} <= names
