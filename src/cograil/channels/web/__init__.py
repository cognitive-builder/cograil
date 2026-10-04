"""The web chat: one static HTML page, served at GET / (ADR 0006).

The page has inline CSS and JavaScript, no framework and no build step. It talks to the API of
cograil.api: POST /chat (server-sent events), GET /runs/{id}, GET and POST /approvals/{token},
GET /auth/me. The page itself needs no sign-in; the page finds out who is signed in by asking
/auth/me, and offers /auth/login — carrying its own address as `next`, so an approval link
survives sign-in — when the answer is 401.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

PAGE = Path(__file__).with_name("chat.html").read_text(encoding="utf-8")

# The page may only talk to its own origin and may not be framed.
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
    return HTMLResponse(PAGE, headers=HEADERS)
