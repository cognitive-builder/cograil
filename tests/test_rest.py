"""kind=rest tests for issue #8: OAuth client credentials, pagination, retry, logging."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cograil.domain import Connection, RestEndpoint, RestPagination, Tool
from cograil.errors import ToolArgumentError, ToolExecutionError
from cograil.tool_kinds.rest import OAuthClientCredentials, RestTool

Handler = Callable[[httpx.Request], httpx.Response]

OAUTH = Connection(
    name="hris",
    auth="oauth_client_credentials",
    base_url="https://hris.test/api",
    token_url="https://login.test/token",
    secret_env={"client_id": "HRIS_ID", "client_secret": "HRIS_SECRET"},
    scopes=["hris.read"],
)


def rest_tool(method: str = "GET", path: str = "/people/{employee}", **kw: Any) -> Tool:
    endpoint = RestEndpoint(method=method, path=path, **kw)  # type: ignore[arg-type]
    return Tool(name="hris.people", kind="rest", scope="read", connection="hris", rest=endpoint)


class Server:
    """A MockTransport that serves tokens and records every API request."""

    def __init__(self, api: Handler) -> None:
        self.api, self.tokens, self.requests = api, 0, list[httpx.Request]()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.test":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"t{self.tokens}", "expires_in": 600})
        self.requests.append(request)
        return self.api(request)

    def tool(self, tool: Tool) -> tuple[RestTool, list[float]]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        auth = OAuthClientCredentials(OAUTH, http, clock=lambda: 0.0)
        return RestTool(tool, OAUTH, http, auth, sleep=sleep), sleeps


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HRIS_ID", "id")
    monkeypatch.setenv("HRIS_SECRET", "secret")


async def test_oauth_token_is_fetched_once_and_sent_as_bearer() -> None:
    server = Server(lambda r: httpx.Response(200, json={"name": r.url.path.rsplit("/", 1)[1]}))
    call, _ = server.tool(rest_tool())
    assert await call({"employee": "alice"}) == {"name": "alice"}
    assert await call({"employee": "bob smith", "verbose": "1"}) == {"name": "bob smith"}
    assert server.tokens == 1
    assert {r.headers["authorization"] for r in server.requests} == {"Bearer t1"}
    assert server.requests[1].url.params["verbose"] == "1"


async def test_401_refreshes_the_token_once() -> None:
    def api(request: httpx.Request) -> httpx.Response:
        ok = request.headers["authorization"] == "Bearer t2"
        return httpx.Response(200 if ok else 401, json={})

    call, _ = Server(api).tool(rest_tool())
    assert await call({"employee": "alice"}) == {}


@pytest.mark.parametrize("cursor_param", ["page", None])
async def test_pagination_collects_items_from_every_page(cursor_param: str | None) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        following = None if page == 3 else page + 1
        if following and cursor_param is None:
            following = f"/api/people?page={following}"
        return httpx.Response(200, json={"data": {"items": [page]}, "next": following})

    pagination = RestPagination(items="data.items", next="next", cursor_param=cursor_param)
    call, _ = Server(api).tool(rest_tool(path="/people", pagination=pagination))
    assert await call({}) == [1, 2, 3]


@pytest.mark.parametrize(
    ("following", "max_pages"), [("https://evil.test/steal", 10), ("/api/people", 2)]
)
async def test_pagination_rejects_foreign_origin_and_unbounded_paging(
    following: str, max_pages: int
) -> None:
    pagination = RestPagination(items="items", next="next", max_pages=max_pages)
    api = lambda r: httpx.Response(200, json={"items": [1], "next": following})  # noqa: E731
    call, _ = Server(api).tool(rest_tool(path="/people", pagination=pagination))
    with pytest.raises(ToolExecutionError):
        await call({})


@pytest.mark.parametrize(
    ("method", "status", "attempts", "sleeps"),
    [
        ("GET", 503, 3, [0.5, 1.0]),  # idempotent: retried with exponential backoff
        ("GET", 404, 1, []),  # client errors are never retried
        ("POST", 503, 1, []),  # a write may have happened: not retried
        ("POST", 429, 3, [0.5, 1.0]),  # rate limited: nothing happened, safe to retry
    ],
)
async def test_retry_with_backoff_only_when_safe(
    method: str, status: int, attempts: int, sleeps: list[float]
) -> None:
    server = Server(lambda r: httpx.Response(status, json={}))
    call, slept = server.tool(rest_tool(method=method, path="/people"))
    with pytest.raises(ToolExecutionError, match=f"HTTP {status}"):
        await call({"name": "x"})
    assert (len(server.requests), slept) == (attempts, sleeps)


async def test_dropped_connection_is_retried_then_succeeds() -> None:
    failures = [httpx.ConnectError("refused")]

    def api(request: httpx.Request) -> httpx.Response:
        if failures:
            raise failures.pop()
        return httpx.Response(200, json={"ok": True})

    call, slept = Server(api).tool(rest_tool(method="POST", path="/leave"))
    assert await call({"days": 2}) == {"ok": True}
    assert slept == [0.5]


async def test_each_attempt_is_logged_as_structured_json_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call, _ = Server(lambda r: httpx.Response(200, json={})).tool(rest_tool())
    with caplog.at_level(logging.INFO, logger="cograil"):
        await call({"employee": "alice"})
    events = [json.loads(r.getMessage()) for r in caplog.records]
    response = next(e for e in events if e["event"] == "tool.rest.response")
    assert response | {"elapsed_ms": 0} == {
        "event": "tool.rest.response", "tool": "hris.people", "method": "GET",
        "path": "/people/{employee}", "status": 200, "attempt": 1, "elapsed_ms": 0,
        "run_id": None,
    }  # fmt: skip
    assert "secret" not in caplog.text and "Bearer" not in caplog.text


@pytest.mark.parametrize("employee", ["..", ".", ""])
async def test_path_arguments_cannot_move_the_call_to_another_endpoint(employee: str) -> None:
    server = Server(lambda r: httpx.Response(200, json={}))
    call, _ = server.tool(rest_tool(method="DELETE", path="/people/{employee}/leave"))
    with pytest.raises(ToolArgumentError):
        await call({"employee": employee})
    assert server.requests == []
