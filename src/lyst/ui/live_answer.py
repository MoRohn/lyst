# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The live answer: Lyst working on a question, shown as it happens, through the AI SDK in the page.

:func:`live_answer` draws a ``st.components.v2`` component (``live_component``, built from ``ui-src/live``) that runs the
AI SDK client in the browser and reads the engine's UI message stream (``POST /v1/workspaces/{ws}/chat``): each step
as it runs, the answer as the model writes it (only figures already backed by Lyst's own numbers, ``lyst.live``),
the datasets it read, and approvals it waits for. A redraw of the page never interrupts a stream that is being read,
and a stream cut by a reload resumes.

The browser holds only a chat ticket (``lyst.tickets``): 15 minutes, this workspace's chat routes, nothing else. The
interface keeps the person's session on the server, as for every other call. ``LYST_PUBLIC_API_URL`` is where the
browser reaches the engine when that differs from where the interface does (``LYST_API_URL``).

Where it sits on each page is the page's own decision; this module only draws it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import streamlit as st

from lyst.ui.client import APIError, client, workspace

RENEW = 120  # seconds before a ticket expires when the interface asks for a new one
LABELS = {"starting": "Starting…", "working": "Working", "done": "Done", "open": "Open answer", "steps": "Steps",
          "sources": "Sources", "approve": "Waiting for approval in Work › Approvals",
          "error": "The live view stopped:"}

_COMPONENT = None


def _component():
    """Registered once per process: the bundled AI SDK client and its styles."""
    global _COMPONENT
    if _COMPONENT is None:
        here = Path(__file__).parent / "live_component"
        from streamlit.components.v2 import component  # noqa: PLC0415

        _COMPONENT = component("lyst_live_answer", css=(here / "live.css").read_text(encoding="utf-8"),
                               js=(here / "live.js").read_text(encoding="utf-8"))
    return _COMPONENT


def public_api() -> str:
    return (os.environ.get("LYST_PUBLIC_API_URL") or os.environ.get("LYST_API_URL") or "http://127.0.0.1:8770").rstrip("/")


def ticket(ws: str, *, now: float | None = None) -> dict[str, Any]:
    """This person's chat ticket for the workspace, renewed shortly before it expires (one request per renewal)."""
    now = time.time() if now is None else now
    held = st.session_state.setdefault("_lyst_chat_tickets", {})
    t = held.get(ws)
    if not t or t["expires_at"] - now < RENEW:
        t = held[ws] = client().post(f"/v1/workspaces/{ws}/chat/tickets")
    return t


def payload(ws: str, chat_id: str, *, ask: str | None, ask_id: str | None) -> dict[str, Any]:
    t = ticket(ws)
    return {"api": public_api() + t["api"], "ticket": t["ticket"], "chatId": chat_id, "ask": ask, "askId": ask_id,
            "labels": LABELS}


def live_answer(chat_id: str, *, ask: str | None = None, ask_id: str | None = None, key: str | None = None) -> dict | None:
    """Draw the live answer for a chat. ``ask`` with a new ``ask_id`` asks it (once per id, however often the page
    redraws). Returns the component's latest event ({type: done|open|error, ...}) or None."""
    ws = workspace()
    try:
        data = payload(ws, chat_id, ask=ask, ask_id=ask_id)
    except APIError as exc:
        st.caption(f"The live view is not available: {exc}")
        return None
    res = _component()(data=data, key=key or f"lyst_live_{chat_id}", on_event_change=lambda: None)
    ev = getattr(res, "event", None)
    return ev if isinstance(ev, dict) else None
