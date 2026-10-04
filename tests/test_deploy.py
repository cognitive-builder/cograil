"""The deploy workflow and Dockerfile carry what issue #25 asks for."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow_text() -> str:
    return (ROOT / ".github/workflows/deploy.yml").read_text()


@pytest.fixture
def workflow(workflow_text: str) -> dict[Any, Any]:
    return yaml.safe_load(workflow_text)  # type: ignore[no-any-return]


def test_dockerfile_is_multi_stage_and_not_root() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    stages = [line for line in lines if line.startswith("FROM ")]
    assert len(stages) >= 2
    assert "USER cograil" in lines
    assert "FROM python:3.12-slim AS runtime" in lines


def test_deploy_runs_on_tag_with_workload_identity_and_no_keys(
    workflow: dict[Any, Any], workflow_text: str
) -> None:
    assert workflow[True]["push"]["tags"] == ["v*"]  # PyYAML reads `on:` as True
    assert workflow["jobs"]["deploy"]["permissions"]["id-token"] == "write"
    assert "workload_identity_provider" in workflow_text
    assert "credentials_json" not in workflow_text
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in workflow_text


def test_deploy_takes_secrets_from_secret_manager_not_github(workflow_text: str) -> None:
    assert "DATABASE_URL=cograil-database-url:latest" in workflow_text
    assert "secrets.DATABASE_URL" not in workflow_text
    assert "secrets.ANTHROPIC_API_KEY" not in workflow_text


def test_deploy_scales_to_zero_and_guards_image_size(workflow_text: str) -> None:
    assert "--min-instances=0" in workflow_text
    assert "MAX_IMAGE_MB" in workflow_text


def test_the_image_does_not_trust_forwarded_headers_by_itself(workflow_text: str) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--forwarded-allow-ips" not in dockerfile  # uvicorn then reads FORWARDED_ALLOW_IPS
    assert "--proxy-headers" in dockerfile  # else the Cloud Run opt-in below would do nothing
    assert "FORWARDED_ALLOW_IPS=*" in workflow_text  # only the Cloud Run service opts in


def test_dockerignore_keeps_secrets_out_of_the_image() -> None:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    for pattern in (".env", ".env.*", "workspaces/*/.secrets"):
        assert pattern in lines
