"""The web chat page (issue #18): one static HTML file served by FastAPI at GET /.

The page's behaviour is exercised in a browser by hand; these tests hold what the server can
promise: how the page is served, that it stays a single dependency-free file, and that it speaks
the events and endpoints the API really has (so the page and the API cannot drift apart)."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_api import Env

from cograil.channels.web import PAGE


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield Env(tmp_path)


def test_the_page_is_served_without_sign_in(env: Env) -> None:
    response = env.client.get("/")  # no X-User: the page finds out who is signed in itself
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == PAGE
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "/" not in env.client.get("/openapi.json").json()["paths"]


def test_the_page_is_one_file_with_no_framework_or_outside_requests() -> None:
    assert PAGE.count("<script") == 1 and PAGE.count("<style") == 1
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|https?://", PAGE)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", PAGE)


@pytest.mark.parametrize("name", ["routed", "refusal", "run", "progress", "done", "error"])
def test_the_page_handles_every_chat_event(name: str) -> None:
    assert f'name === "{name}"' in PAGE


def test_the_page_uses_the_endpoints_the_api_has(env: Env) -> None:
    paths = env.client.get("/openapi.json").json()["paths"]
    used = {"/chat", "/runs/{id}", "/approvals/{token}", "/auth/me"}
    assert used <= {p.replace("{run_id}", "{id}") for p in paths}
    for call in ('api("/chat"', "`/runs/${", "`/approvals/${", 'api("/auth/me")'):
        assert call in PAGE


def test_the_approval_card_posts_only_the_decision() -> None:
    assert 'dataset.decision = "approved"' in PAGE and 'dataset.decision = "declined"' in PAGE
    assert "JSON.stringify({ decision })" in PAGE


def test_the_page_fits_a_phone() -> None:
    assert 'name="viewport" content="width=device-width, initial-scale=1' in PAGE
    assert "font-size: 16px" in PAGE and "min-height: 44px" in PAGE
    assert "100dvh" in PAGE
