# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A Lyst App in the page (``lyst.apps``): generated block by block as Lyst writes it, or opened from a saved version.

:func:`app_view` draws a ``st.components.v2`` component (``apps_component``, built from ``ui-src/live``). Its host part
runs the AI SDK client and reads ``POST /v1/workspaces/{ws}/apps/generate``, or opens ``GET .../apps/{id}/open``; the
app itself is drawn in an iframe with ``sandbox="allow-scripts"`` (no same origin) under a CSP of ``default-src
'none'``, so the renderer has no network, no storage and no access to the page, and receives only the checked spec
and the values Lyst resolved. The browser holds only a chat ticket (``lyst.tickets``), as for the live answer.

Where an app sits on each page is the page's own decision; this module only draws it.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from lyst.ui.client import APIError, workspace
from lyst.ui.live_answer import public_api, ticket

LABELS = {"building": "Building the app…", "opening": "Opening…", "saved": "Saved", "version": "Version",
          "notSaved": "Not saved: editors save apps", "from": "From", "leftOut": "left out",
          "error": "The app stopped:", "vsPrior": "vs prior period", "noData": "Not enough data yet.", "by": "by",
          "filter": "Filter", "all": "All", "change": "change", "onLatest": "on the latest value",
          "projection": "Projection, not data"}

_COMPONENT = None


def _component():
    """Registered once per process: the bundled host (AI SDK client and the sandboxed renderer) and its styles."""
    global _COMPONENT
    if _COMPONENT is None:
        here = Path(__file__).parent / "apps_component"
        from streamlit.components.v2 import component  # noqa: PLC0415

        _COMPONENT = component("lyst_app_view", css=(here / "apps.css").read_text(encoding="utf-8"),
                               js=(here / "apps.js").read_text(encoding="utf-8"))
    return _COMPONENT


def payload(ws: str, *, ask: str | None, ask_id: str | None, app_id: str | None) -> dict:
    t = ticket(ws)
    return {"apps": public_api() + t.get("apps", f"/v1/workspaces/{ws}/apps"), "ticket": t["ticket"], "ask": ask,
            "askId": ask_id, "appId": app_id, "labels": LABELS}


def app_view(*, ask: str | None = None, ask_id: str | None = None, app_id: str | None = None,
             key: str | None = None) -> dict | None:
    """Draw an app. ``ask`` with a new ``ask_id`` generates one from the sentence (once per id, however often the page
    redraws); ``app_id`` opens a saved app with its values read now. Returns the latest event
    ({type: done, appId, saved, version} or {type: error, message}) or None."""
    ws = workspace()
    try:
        data = payload(ws, ask=ask, ask_id=ask_id, app_id=app_id)
    except APIError as exc:
        st.caption(f"The app is not available: {exc}")
        return None
    res = _component()(data=data, key=key or f"lyst_app_{ask_id or app_id}", on_event_change=lambda: None)
    ev = getattr(res, "event", None)
    return ev if isinstance(ev, dict) else None
