# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Home's composition: what the agent bar shows when it *is* Home (nothing open, ``?task=`` absent).

Home is one focal point: Lyst's mark, one line, one composer, at most three starters, the single thing that most
wants the person, and a quiet dock (work in flight, what waits, recent work). Everything here is chosen by Lyst from
the workspace's own records, never a fixed list, and everything else (activity, artifacts, older answers) is one
gesture away in the dock, the bar's panel or Brain › Work.
"""

from __future__ import annotations

import time
from typing import Any

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace

TTL = 20  # seconds: the starters and recent work are re-read at most this often per workspace
TITLE = "What should Lyst work on?"
PLACEHOLDER = "Ask anything: why something changed, a forecast, a what-if"
STARTERS = 3
RECENT = 6
ART_KIND = {"dashboard": "dashboard", "briefing": "briefing", "report": "report", "presentation": "presentation",
            "workbook": "workbook", "workflow": "workflow"}


def suggestions(metrics: list[dict]) -> list[str]:
    """Questions this workspace can answer well, from its own approved metrics, not a hard-coded list."""
    out: list[str] = []
    # ratios first: "why did it change" is the question a measure of performance invites
    metrics = sorted(metrics, key=lambda m: not (m.get("kind") == "ratio" or m.get("format") == "percent"))
    for m in metrics[:4]:
        t = kit.lower(m["title"])
        if m.get("kind") == "ratio" or m.get("format") == "percent":
            out.append(f"Why did {t} change last quarter?")
        elif m.get("dimensions"):
            out.append(f"What was {t} last quarter by {m['dimensions'][0].replace('_', ' ')}?")
        else:
            out.append(f"Forecast {t}")
    if metrics:
        out.append(f"Forecast {kit.lower(metrics[0]['title'])}")
    return list(dict.fromkeys(out))[:5]


def about(metrics: list[dict]) -> str:
    names = ", ".join(m["title"] for m in metrics[:3]) + (f" and {len(metrics) - 3} more" if len(metrics) > 3 else "")
    return (f"Lyst can answer about {len(metrics)} approved measure{'s' * (len(metrics) != 1)} here ({names}): why "
            "they changed, what drove them, a breakdown, a trend or a forecast.") if metrics else ""


def is_home(page: str) -> bool:
    """Home with nothing open: the bar becomes the page."""
    return page == "home" and not st.query_params.get("task")  # the URL says what Home has open (analyst.py)


def _read(ws: str) -> dict[str, Any]:
    hit = st.session_state.get("_home")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < TTL:
        return hit[2]
    api = client()
    out: dict[str, Any] = {"metrics": [], "datasets": None, "pins": [], "onboarding": None, "blocked": []}
    try:
        out["metrics"] = api.get(f"/v1/workspaces/{ws}/metrics") or []
        out["datasets"] = api.get(f"/v1/workspaces/{ws}/datasets") or []
    except APIError:
        pass
    try:
        out["pins"] = (api.get(f"/v1/workspaces/{ws}/pins") or [])[:3]
    except APIError:
        pass
    try:
        out["onboarding"] = api.get(f"/v1/workspaces/{ws}/onboarding")
    except APIError:
        pass
    try:
        out["blocked"] = [a for a in api.get(f"/v1/workspaces/{ws}/anticipations", status="open") or []
                          if a.get("verdict") == "blocked" and a.get("kind") != "next_best_action"]
    except APIError:
        pass
    st.session_state["_home"] = (ws, time.monotonic(), out)
    return out


def drop() -> None:
    st.session_state.pop("_home", None)


def _setup(ob: dict | None) -> dict | None:
    """Get started, as one dock item while anything required is left (the guide opens in a sheet)."""
    if not ob or ob.get("state") == "complete" or st.session_state.get("hide_get_started"):
        return None
    steps = ob.get("steps") or []
    need = sum(not s["optional"] for s in steps)
    done = sum(s["done"] for s in steps if not s["optional"])
    return {"done": done, "need": need} if need and done < need else None


def _focus(tasks: list[dict], blocked: list[dict], has_data: bool) -> dict | None:
    """The one thing Lyst most wants the person to see, or nothing. Never a list."""
    if not has_data:
        return {"kicker": "First", "title": "Add a spreadsheet or connect a database", "tone": "",
                "go": {"page": "app_pages/data.py", "params": {"add": "1"}}}
    for t in tasks[:30]:
        if t.get("outcome") == "needs_input":
            return {"kicker": "Needs you", "title": t.get("title") or "A question", "task": t["id"], "tone": "warn"}
    for t in tasks[:30]:
        if t.get("outcome") == "unverified":
            return {"kicker": "Did not verify", "title": t.get("title") or "An answer", "task": t["id"], "tone": "warn"}
    if blocked:
        a = blocked[0]
        return {"kicker": "Blocked", "title": a.get("objective") or "An upcoming run", "tone": "warn",
                "go": {"page": "app_pages/brain.py", "params": {"view": "work", "work": "needs you"}}}
    return None


def _recent(tasks: list[dict], pins: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in pins:
        kind = p.get("kind")
        go = ({"page": "app_pages/data.py", "params": {"dataset": p["id"]}} if kind == "dataset" else
              {"page": "app_pages/brain.py", "params": {"metric": p["id"]}} if kind == "metric" else
              {"page": "app_pages/artifact.py", "params": {"kind": kind, "id": p["id"]}})
        out.append({"title": p.get("title") or p["id"], "kind": "pin", "go": go, "label": "Pinned"})
    for t in tasks:
        if len(out) >= RECENT:
            break
        if t.get("kind", "analysis") != "analysis" or t["status"] not in ("done", "failed"):
            continue
        out.append({"title": t.get("title") or "Untitled", "kind": "answer", "task": t["id"],
                    "when": kit.when(t.get("updated_at") or t.get("created_at"))})
    return out


def payload(tasks: list[dict]) -> dict[str, Any]:
    """What the bar draws as Home."""
    d = _read(workspace())
    metrics = d["metrics"]
    has_data = bool(d["datasets"]) if d["datasets"] is not None else True
    starters = [{"text": q} for q in suggestions(metrics)[:STARTERS]] if has_data else []
    return {"title": TITLE, "placeholder": PLACEHOLDER, "starters": starters, "about": about(metrics),
            "focus": _focus(tasks, d["blocked"], has_data), "recent": _recent(tasks, d["pins"]),
            "setup": _setup(d["onboarding"])}


__all__ = ["TITLE", "about", "drop", "is_home", "payload", "suggestions"]
