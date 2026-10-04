"""kind=rest: the enterprise connector pattern over httpx.

OAuth client credentials with a cached token, pagination by a field in the JSON body,
retry with exponential backoff, and one structured log event per HTTP attempt.
Only safe retries happen: 429 always; 5xx and dropped connections only for idempotent
methods; a connection that never opened for any method.
"""

from __future__ import annotations

import asyncio
import os
import string
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from cograil.domain import Connection, RestPagination, Tool
from cograil.errors import ToolArgumentError, ToolConfigError, ToolExecutionError
from cograil.observability import log_event

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

_IDEMPOTENT = frozenset({"GET", "PUT", "DELETE"})
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_TOKEN_MARGIN_S = 30.0


class OAuthClientCredentials:
    """Fetches and caches a bearer token for one Connection; secrets come from env vars."""

    def __init__(self, connection: Connection, http: httpx.AsyncClient, clock: Clock) -> None:
        self._connection = connection
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at:
                await self._fetch()
            return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> None:
        self._token = None

    async def _fetch(self) -> None:
        conn = self._connection
        if conn.token_url is None:
            raise ToolConfigError(f"connection {conn.name}: token_url is not set")
        form = {"grant_type": "client_credentials"}
        form |= {key: _secret(conn, key) for key in ("client_id", "client_secret")}
        if conn.scopes:
            form["scope"] = " ".join(conn.scopes)
        response = await self._http.post(conn.token_url, data=form)
        log_event("tool.rest.token", connection=conn.name, status=response.status_code)
        token = _json(response).get("access_token") if not response.is_error else None
        if not isinstance(token, str):
            raise ToolExecutionError(
                f"connection {conn.name}: token request failed with HTTP {response.status_code}"
            )
        expires_in = float(_json(response).get("expires_in", 3600))
        self._token = token
        self._expires_at = self._clock() + max(0.0, expires_in - _TOKEN_MARGIN_S)


class RestTool:
    """Invokes one REST endpoint of a Connection."""

    def __init__(
        self,
        tool: Tool,
        connection: Connection,
        http: httpx.AsyncClient,
        auth: OAuthClientCredentials | None,
        sleep: Sleep = asyncio.sleep,
        backoff_s: float = 0.5,
    ) -> None:
        if tool.rest is None or not connection.base_url:
            raise ToolConfigError(f"{tool.name}: needs a rest block and a Connection base_url")
        self._tool, self._endpoint, self._http = tool, tool.rest, http
        self._base = connection.base_url.rstrip("/") + "/"
        self._auth, self._sleep, self._backoff_s = auth, sleep, backoff_s

    async def __call__(self, args: dict[str, Any]) -> Any:
        path, rest = _fill_path(self._tool.name, self._endpoint.path, args)
        url = urljoin(self._base, path.lstrip("/"))
        query, body = (rest, None) if self._endpoint.method in ("GET", "DELETE") else ({}, rest)
        if self._endpoint.pagination is None:
            return _body(await self._request(url, query, body))
        return await self._paginate(url, query, body, self._endpoint.pagination)

    async def _paginate(
        self, url: str, query: dict[str, Any], body: Any, page: RestPagination
    ) -> list[Any]:
        items: list[Any] = []
        for _ in range(page.max_pages):
            data = _json(await self._request(url, query, body))
            found = _dig(data, page.items)
            if not isinstance(found, list):
                raise ToolExecutionError(f"{self._tool.name}: response has no '{page.items}' list")
            items.extend(found)
            following = _dig(data, page.next)
            if not following:
                return items
            if page.cursor_param:
                query = {**query, page.cursor_param: following}
            else:
                url, query = self._same_origin(urljoin(url, str(following))), {}
        raise ToolExecutionError(f"{self._tool.name}: more than {page.max_pages} pages")

    def _same_origin(self, url: str) -> str:
        base, target = urlsplit(self._base), urlsplit(url)
        if (base.scheme, base.netloc) != (target.scheme, target.netloc):
            raise ToolExecutionError(f"{self._tool.name}: next page points to another origin")
        return url

    async def _request(self, url: str, query: dict[str, Any], body: Any) -> httpx.Response:
        response = await self._with_retry(lambda attempt: self._send(url, query, body, attempt))
        if response.is_error:
            raise ToolExecutionError(
                f"{self._tool.name}: {self._endpoint.method} "
                f"{self._endpoint.path} returned HTTP {response.status_code}"
            )
        return response

    async def _with_retry(self, send: Callable[[int], Awaitable[httpx.Response]]) -> httpx.Response:
        method = self._endpoint.method
        for attempt in range(1, self._endpoint.max_attempts):
            try:
                response = await send(attempt)
            except httpx.TransportError as exc:
                if not (isinstance(exc, _NOT_SENT) or method in _IDEMPOTENT):
                    raise self._failed(exc) from exc
                reason = type(exc).__name__
            else:
                if not _retryable(method, response.status_code):
                    return response
                reason = f"HTTP {response.status_code}"
            delay = self._backoff_s * 2 ** (attempt - 1)
            log_event(
                "tool.rest.retry",
                tool=self._tool.name,
                attempt=attempt,
                reason=reason,
                delay_s=delay,
            )
            await self._sleep(delay)
        try:
            return await send(self._endpoint.max_attempts)
        except httpx.TransportError as exc:
            raise self._failed(exc) from exc

    async def _send(
        self, url: str, query: dict[str, Any], body: Any, attempt: int
    ) -> httpx.Response:
        response = await self._send_once(url, query, body, attempt)
        if response.status_code == 401 and self._auth is not None:
            self._auth.invalidate()
            response = await self._send_once(url, query, body, attempt)
        return response

    async def _send_once(
        self, url: str, query: dict[str, Any], body: Any, attempt: int
    ) -> httpx.Response:
        headers = await self._auth.headers() if self._auth is not None else {}
        started = time.monotonic()
        response = await self._http.request(
            self._endpoint.method, url, params=query or None, json=body, headers=headers
        )
        log_event(
            "tool.rest.response",
            tool=self._tool.name,
            method=self._endpoint.method,
            path=self._endpoint.path,
            status=response.status_code,
            attempt=attempt,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        return response

    def _failed(self, exc: httpx.TransportError) -> ToolExecutionError:
        return ToolExecutionError(
            f"{self._tool.name}: {type(exc).__name__} calling "
            f"{self._endpoint.method} {self._endpoint.path}"
        )


def _retryable(method: str, status: int) -> bool:
    return status == 429 or (status >= 500 and method in _IDEMPOTENT)


def _fill_path(tool: str, path: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    names = {field for _, field, _, _ in string.Formatter().parse(path) if field}
    missing = names - args.keys()
    if missing:
        raise ToolArgumentError(f"{tool}: missing path arguments {sorted(missing)}")
    filled = path.format_map({name: quote(str(args[name]), safe="") for name in names})
    return filled, {key: value for key, value in args.items() if key not in names}


def _secret(connection: Connection, key: str) -> str:
    env = connection.secret_env[key]
    value = os.environ.get(env)
    if not value:
        raise ToolConfigError(f"connection {connection.name}: env var {env} is not set")
    return value


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolExecutionError(f"{response.request.url.path}: response is not JSON") from exc
    if not isinstance(data, dict):
        raise ToolExecutionError(f"{response.request.url.path}: expected a JSON object")
    return data


def _body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    if "json" in response.headers.get("content-type", ""):
        return response.json()
    return response.text


def _dig(data: dict[str, Any], dotted: str) -> Any:
    found: Any = data
    for key in dotted.split("."):
        found = found.get(key) if isinstance(found, dict) else None
    return found
