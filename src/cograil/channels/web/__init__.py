"""The web pages (ADR 0006): each one a static HTML file, inline CSS and JavaScript, no
framework and no build step, served by FastAPI.

The chat at GET / talks to the API of cograil.api: POST /chat (server-sent events),
GET /runs/{id}, GET and POST /approvals/{token}, GET /auth/me. The run history at GET /history
is read-only: it GETs /runs for the list and /runs/{id} for a Run's Steps, tool calls, gates
and timings. Neither page needs a sign-in to load; each asks /auth/me who is signed in and
offers /auth/login — carrying its own address as `next`, so an approval link or an open Run
survives sign-in — when the answer is 401.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

CHAT_PAGE = Path(__file__).with_name("chat.html").read_text(encoding="utf-8")
HISTORY_PAGE = Path(__file__).with_name("history.html").read_text(encoding="utf-8")

# The pages may only talk to their own origin and may not be framed.
HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def chat_page() -> HTMLResponse:
    """The web chat. Open `/?approval=<token>` to land on an approval card."""
    return HTMLResponse(CHAT_PAGE, headers=HEADERS)


@router.get("/history", response_class=HTMLResponse, include_in_schema=False)
async def history_page() -> HTMLResponse:
    """The run history: every Run of the signed-in Principal, read-only."""
    return HTMLResponse(HISTORY_PAGE, headers=HEADERS)
