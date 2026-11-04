# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The command palette: one keyboard-first place to go anywhere, find anything, or start work.

Press Ctrl/⌘+K (or the sidebar's search button). One field answers:

* **Ask Lyst**: send what you typed as a question; the answer opens on Home.
* **Go to**: every area and section, by name (so nothing needs a top-level link of its own).
* **Recent**: your latest questions with their real status from the engine.
* **Found**: datasets, metrics, columns, documents, saved analyses and dashboards (the catalog search).
* **Expertise**: matching Analyst specializations with their measured maturity, and an example to ask.
* **Workspaces**: switch to another workspace you belong to.

Everything comes from the API at the moment the palette is open; nothing here is cached across workspaces, so a
switch never shows another workspace's items.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client
from lyst.ui.components import task_state

# (area, section, page, query) — every place a person can go, searchable by its words
PLACES: list[tuple[str, str | None, str, dict[str, str]]] = [
    ("Home", None, "app_pages/analyst.py", {}),
    ("Work", "Needs you", "app_pages/work.py", {"view": "needs you"}),
    ("Work", "Activity", "app_pages/work.py", {"view": "activity"}),
    ("Work", "Recurring", "app_pages/work.py", {"view": "recurring"}),
    ("Data", "Datasets", "app_pages/data.py", {"view": "datasets"}),
    ("Data", "Dashboards", "app_pages/data.py", {"view": "dashboards"}),
    ("Data", "Saved analyses", "app_pages/data.py", {"view": "saved analyses"}),
    ("Data", "SQL", "app_pages/data.py", {"view": "sql"}),
    ("Brain", "Definitions", "app_pages/brain.py", {"view": "definitions"}),
    ("Brain", "Documents", "app_pages/brain.py", {"view": "documents"}),
    ("Brain", "Memory", "app_pages/brain.py", {"view": "memory"}),
    ("Settings", "Models", "app_pages/settings.py", {"view": "models"}),
    ("Settings", "Connections", "app_pages/settings.py", {"view": "connections"}),
    ("Settings", "Workspace", "app_pages/settings.py", {"view": "workspace"}),
    ("Settings", "Advanced", "app_pages/settings.py", {"view": "advanced"}),
]
# extra words people use for a place, so "approvals" finds Needs you and "voice" finds Settings
ALIASES = {"Needs you": "approvals reviews inbox assigned questions", "Activity": "tasks history runs",
           "Recurring": "schedules monitors watch repeat", "Datasets": "tables sources upload connect add data",
           "SQL": "query workbench", "Definitions": "metrics measures terms glossary",
           "Models": "openrouter llm model key", "Workspace": "voice audio usage limits webhooks members",
           "Advanced": "skills capabilities evals", "Connections": "credentials secrets"}

KINDS = {"metric": ("Metric", ":material/functions:"), "dataset": ("Dataset", ":material/database:"),
         "column": ("Column", ":material/view_column:"), "term": ("Business term", ":material/menu_book:"),
         "workflow": ("Saved analysis", ":material/bolt:"), "dashboard": ("Dashboard", ":material/dashboard:"),
         "analysis": ("Earlier answer", ":material/chat:"), "document": ("Document", ":material/description:")}


def destination(hit: dict) -> tuple[str, dict]:
    """Where a search result lives in the five areas, as a page and the query that opens it."""
    kind, name = hit["kind"], hit["name"]
    if kind == "dataset":
        return "app_pages/data.py", {"dataset": name}
    if kind == "column":
        return "app_pages/data.py", {"dataset": name.split(".")[0]}
    if kind == "metric":
        return "app_pages/brain.py", {"metric": name}
    if kind == "term":
        return "app_pages/brain.py", {}
    if kind == "document":
        return "app_pages/brain.py", {"view": "documents"}
    if kind == "workflow":
        return "app_pages/data.py", {"view": "saved analyses"}
    if kind == "dashboard":
        return "app_pages/data.py", {"view": "dashboards"}
    return "app_pages/analyst.py", {"task": name}


def places(q: str) -> list[tuple[str, str | None, str, dict[str, str]]]:
    """The places whose name or usual words contain every word typed."""
    words = q.lower().split()
    if not words:
        return []
    out = []
    for p in PLACES:
        hay = f"{p[0]} {p[1] or ''} {ALIASES.get(p[1] or '', '')}".lower()
        if all(w in hay for w in words):
            out.append(p)
    return out


def _open_task(tid: str) -> None:
    st.session_state.task_id = tid
    st.switch_page("app_pages/analyst.py", query_params={"task": tid})


def _section(title: str, n: int) -> None:
    kit.label(f"{title} · {n}" if n > 1 else title)


@st.dialog("Go to", width="large")
def palette(spaces: list[dict]) -> None:
    api = client()
    ws = st.session_state.workspace
    q = (st.text_input("Search or ask", key="palette_q", placeholder="Type a place, a dataset, a metric, or a question",
                       label_visibility="collapsed", icon=":material/search:", live=True) or "").strip()
    st.caption("Type a question and choose Ask, or pick a place · Esc closes")

    if len(q) >= 3:
        if kit.row(f"Ask Lyst: “{q}”", "Lyst works it out from approved data and checks every figure",
                   actions=[("Ask", "ask")], key="pal_ask", slot=False):
            out = kit.guarded(lambda: api.ask(ws, q))
            if out:
                _open_task(out["task_id"])

    hits = places(q)
    if hits:
        _section("Go to", len(hits))
        for i, (area, sec, page, qp) in enumerate(hits[:6]):
            if kit.row(f"{area} › {sec}" if sec else area, "", actions=[("Open", "go")], key=f"pal_p{i}",
                       divider=False, slot=False):
                st.switch_page(page, query_params=qp)

    if not q:  # nothing typed: what you were last working on, with the engine's status
        try:
            tasks = api.get(f"/v1/workspaces/{ws}/tasks")[:5]
        except APIError:
            tasks = []
        if tasks:
            _section("Recent", len(tasks))
            for t in tasks:
                label, _, _ = task_state(t["status"], t.get("outcome"))
                if kit.row(t.get("title") or "Untitled", f"{label} · {kit.when(t.get('updated_at'))}",
                           actions=[("Open", "go")], key=f"pal_t{t['id']}", divider=False, slot=False):
                    _open_task(t["id"])
        _section("Go to", len(PLACES))
        cols = st.columns(3)
        for i, (area, sec, page, qp) in enumerate(PLACES):
            if cols[i % 3].button(f"{area} › {sec}" if sec else area, key=f"pal_all{i}", type="tertiary"):
                st.switch_page(page, query_params=qp)

    if len(q) >= 2:
        _found(api, ws, q)
        _expertise(api, ws, q)

    others = [w for w in spaces if w["id"] != ws and (not q or q.lower() in w["name"].lower())]
    if others and (q or len(spaces) > 1):
        _section("Workspaces", len(others))
        for w in others[:5]:
            if kit.row(w["name"], w.get("role", ""), actions=[("Switch", "sw")], key=f"pal_w{w['id']}",
                       divider=False, slot=False):
                st.session_state["_switch_ws"] = w["id"]
                st.rerun()


def _found(api: Any, ws: str, q: str) -> None:
    try:
        hits = api.get(f"/v1/workspaces/{ws}/catalog/search", q=q)["results"][:6]
    except APIError:
        return
    if not hits:
        return
    _section("Found", len(hits))
    for i, h in enumerate(hits):
        label, _ = KINDS.get(h["kind"], (h["kind"].capitalize(), None))
        if kit.row(h["title"], f"{label} · {h.get('why') or ''}", actions=[("Open", "go")], key=f"pal_f{i}",
                   divider=False, slot=False):
            page, qp = destination(h)
            if page.endswith("analyst.py"):
                st.session_state.task_id = qp["task"]
            st.switch_page(page, query_params=qp)


def _expertise(api: Any, ws: str, q: str) -> None:
    """Specializations whose description matches. The level is the catalog's measured maturity, shown as is: a
    Defined profile is described, not proven, so the palette offers its example question and nothing more."""
    try:
        found = api.get("/v1/specializations", q=q)[:3]
    except APIError:
        return
    if not found:
        return
    _section("Expertise", len(found))
    for s in found:
        meta = f"{s.get('subdomain') or s.get('domain', '')} · maturity: {s.get('level_label', 'unknown')}"
        if s.get("example"):
            meta += f" · e.g. “{s['example']}”"
        if kit.row(s["name"], meta, actions=[("Ask example", "ex")] if s.get("example") else [],
                   key=f"pal_s{s['id']}", divider=False, slot=False):
            out = kit.guarded(lambda s=s: api.ask(ws, s["example"]))
            if out:
                _open_task(out["task_id"])


def trigger(spaces: list[dict]) -> None:
    """The sidebar's search button; Ctrl/⌘+K opens the same palette from anywhere."""
    if st.button("Search or go to", key="lyst_palette", icon=":material/search:", width="stretch",
                 shortcut="Mod+K", help="Search, jump to any section, or ask (Ctrl/⌘+K)"):
        palette(spaces)
