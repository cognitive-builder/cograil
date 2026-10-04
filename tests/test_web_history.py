"""The run history page (issue #26): one static HTML file served by FastAPI at GET /history.

Like the chat page's, these tests hold what the server can promise: how the page is served,
that it stays a single dependency-free file, that it is read-only, and that it speaks the
endpoints and fields the API really has (so the page and the API cannot drift apart)."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_api import Env

from cograil.channels.web import HISTORY_PAGE


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield Env(tmp_path)


def test_the_page_is_served_without_sign_in(env: Env) -> None:
    response = env.client.get("/history")  # no X-User: the page finds out who is signed in itself
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == HISTORY_PAGE
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "/history" not in env.client.get("/openapi.json").json()["paths"]


def test_the_page_is_one_file_with_no_framework_or_outside_requests() -> None:
    assert HISTORY_PAGE.count("<script") == 1 and HISTORY_PAGE.count("<style") == 1
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|https?://", HISTORY_PAGE)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", HISTORY_PAGE)


def test_the_page_is_read_only() -> None:
    # A history page that could write would be a second way to decide a gate.
    assert not re.search(r"method:|\.post\(|\.put\(|\.patch\(|\.delete\(", HISTORY_PAGE)
    assert "approvals" not in HISTORY_PAGE


def test_the_page_uses_the_endpoints_the_api_has(env: Env) -> None:
    paths = env.client.get("/openapi.json").json()["paths"]
    used = {"/runs", "/runs/{id}", "/auth/me"}
    assert used <= {p.replace("{run_id}", "{id}") for p in paths}
    for call in ('api("/runs")', "`/runs/${", 'api("/auth/me")'):
        assert call in HISTORY_PAGE


def test_the_list_shows_status_protocol_principal_and_cost() -> None:
    for field in ("status", "protocol", "principal_id", "cost_usd"):
        assert field in HISTORY_PAGE


def test_the_drill_down_shows_steps_calls_gates_and_timings() -> None:
    for field in ("steps", "calls", "gates", "created_at", "updated_at", "started_at", "ended_at"):
        assert field in HISTORY_PAGE


def test_sign_in_keeps_the_address() -> None:
    assert '"/auth/login?next=" + encodeURIComponent(location.pathname + location.search)' in (
        HISTORY_PAGE
    )


def test_the_page_fits_a_phone() -> None:
    assert 'name="viewport" content="width=device-width, initial-scale=1' in HISTORY_PAGE
    assert "min-height: 44px" in HISTORY_PAGE and "100dvh" in HISTORY_PAGE
