# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Chat tickets: what a browser holds so the AI SDK can ask Lyst and watch the answer stream, and nothing more.

The interface runs on the server and holds the person's session; the browser never sees it. To stream an answer live,
the interface asks the engine for a ticket (``POST /v1/workspaces/{ws}/chat/tickets``, signed in as the person) and
hands only the ticket to the page. A ticket:

* works only on the chat routes (``POST /v1/workspaces/{ws}/chat`` and its resume ``GET``), only for the workspace it
  names; every other route refuses it, because it is not a session token and the session check never sees it;
* acts as the person who asked for it, so every permission check (``analyst.ask``, ``workspace.read``) still runs on
  each request, and a person removed from the workspace loses access at once;
* expires after ``TTL`` seconds (15 minutes; the interface issues a fresh one on every redraw);
* is an HMAC-SHA256 signature over its claims with a key that exists only in this engine process's memory, so it is
  never stored, and a restart invalidates every ticket.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

TTL = 900
SCHEME = "Ticket"
LABEL = "chat-ticket"  # Principal.token_label of a request made with a ticket  # noqa: S105 - a label, not a secret
_KEY = secrets.token_bytes(32)


class TicketInvalid(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(user_id: str, workspace_id: str, *, ttl: int = TTL, now: float | None = None, key: bytes = _KEY) -> dict[str, Any]:
    expires = int((now if now is not None else time.time()) + max(1, min(ttl, TTL)))
    claims = _b64(json.dumps({"u": user_id, "w": workspace_id, "x": expires, "n": secrets.token_hex(8)},
                             separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, claims.encode(), hashlib.sha256).digest())
    return {"ticket": f"{claims}.{sig}", "expires_at": expires, "scheme": SCHEME}


def verify(ticket: str, workspace_id: str, *, now: float | None = None, key: bytes = _KEY) -> str:
    """The user id the ticket acts for, if it is genuine, unexpired and for this workspace."""
    try:
        claims, sig = ticket.split(".")
        good = hmac.compare_digest(_unb64(sig), hmac.new(key, claims.encode(), hashlib.sha256).digest())
        data = json.loads(_unb64(claims)) if good else None
    except (ValueError, TypeError):
        raise TicketInvalid("this chat ticket is not valid") from None
    if not data:
        raise TicketInvalid("this chat ticket is not valid")
    if data.get("w") != workspace_id:
        raise TicketInvalid("this chat ticket is for another workspace")
    if int(data.get("x") or 0) <= (now if now is not None else time.time()):
        raise TicketInvalid("this chat ticket has expired")
    return str(data["u"])
