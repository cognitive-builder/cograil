"""Service limits (issue #37): a per-principal rate limit on POST /chat and a request body cap.

The message cap of every channel is tested with its channel (test_api.py, test_slack.py) and the
knowledge chunk cap with chunking (test_knowledge.py)."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_api import ALICE, MANAGER, Env

from cograil.api.limits import (
    DEFAULT_CHAT_RATE,
    MAX_BODY_BYTES,
    RateLimit,
    RateLimiter,
    chat_rate_limit,
)
from cograil.errors import LimitNotConfigured


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield Env(tmp_path, chat_rate=RateLimit(requests=2, window_seconds=60))


def runs(env: Env) -> int:
    return len(asyncio.run(env.store.list_runs()))


# The chat rate limit


def test_a_principal_over_the_chat_rate_limit_gets_a_429_and_starts_nothing(env: Env) -> None:
    env.paused_run()
    env.paused_run()
    response = env.client.post("/chat", json={"message": "record 42"}, headers={"X-User": ALICE})
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert "at most 2 in 60 seconds" in response.json()["detail"]
    assert runs(env) == 2


def test_the_rate_limit_counts_each_principal_on_their_own(env: Env) -> None:
    env.paused_run()
    env.paused_run()
    env.route_to("helper/record_item")
    env.run_scripts.append(env.script("- {text: Found 42, done: true}\n"))
    assert dict(env.chat(as_=MANAGER))["run"]
    assert runs(env) == 3


def test_without_a_rate_limit_chat_is_not_counted(tmp_path: Path) -> None:
    env = Env(tmp_path, chat_rate=None)
    for _ in range(3):
        env.paused_run()
    assert runs(env) == 3


def test_the_window_slides_and_a_refused_hit_is_not_counted() -> None:
    now = [0.0]
    limiter = RateLimiter(RateLimit(requests=2, window_seconds=10), clock=lambda: now[0])
    assert limiter.hit("a") is None
    now[0] = 4.0
    assert limiter.hit("a") is None
    assert limiter.hit("a") == 6.0  # the first hit leaves the window at t=10
    now[0] = 9.0
    assert limiter.hit("a") == 1.0
    now[0] = 10.0
    assert limiter.hit("a") is None  # refused hits did not push the window on
    assert limiter.hit("a") == 4.0
    assert limiter.hit("b") is None


@pytest.mark.parametrize(
    ("value", "limit"),
    [
        (None, DEFAULT_CHAT_RATE),
        ("", DEFAULT_CHAT_RATE),
        ("off", None),
        ("OFF", None),
        ("5/10", RateLimit(requests=5, window_seconds=10)),
    ],
)
def test_the_chat_rate_limit_is_read_from_the_environment(
    value: str | None, limit: RateLimit | None
) -> None:
    env = {} if value is None else {"COGRAIL_CHAT_RATE_LIMIT": value}
    assert chat_rate_limit(env) == limit


@pytest.mark.parametrize("value", ["20", "0/60", "20/0", "-1/60", "20/minute", "a/b", "2/3/4"])
def test_a_bad_chat_rate_limit_stops_the_service(value: str) -> None:
    with pytest.raises(LimitNotConfigured, match="COGRAIL_CHAT_RATE_LIMIT"):
        chat_rate_limit({"COGRAIL_CHAT_RATE_LIMIT": value})


# The body cap


def test_a_body_declared_over_the_cap_gets_a_413(env: Env) -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)
    response = env.client.post(
        "/chat", content=body, headers={"X-User": ALICE, "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert runs(env) == 0


def test_a_body_streamed_over_the_cap_gets_a_413(env: Env) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"message": "'
        for _ in range(MAX_BODY_BYTES // 1024 + 1):
            yield b"x" * 1024
        yield b'"}'

    response = env.client.post(
        "/chat", content=chunks(), headers={"X-User": ALICE, "Content-Type": "application/json"}
    )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert runs(env) == 0
