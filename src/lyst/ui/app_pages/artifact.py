# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The artifact viewer: one analysis, chart, dashboard, saved analysis, briefing, deck or workbook, opened on its
own with room to work. Not a navigation area: it is reached from wherever the artifact is shown, and Back returns
there. The address (``?kind=&id=``) is the artifact's stable id, so a reload or a shared link opens the same thing,
and every read goes through the API, so permissions apply exactly as on the page it came from."""

import streamlit as st

from lyst.ui import artifacts, kit
from lyst.ui.client import APIError, client, workspace

api, ws = client(), workspace()
kind = st.query_params.get("kind") or ""
aid = st.query_params.get("id") or ""
origin = st.session_state.get("_artifact_from") or ""


def back() -> None:
    """Return to where the artifact was opened from (its page and query), or Home."""
    from urllib.parse import parse_qsl, urlparse  # noqa: PLC0415

    u = urlparse(origin)
    path = (u.path or "/").strip("/")
    page = {"": "app_pages/analyst.py", "work": "app_pages/work.py", "data": "app_pages/data.py",
            "brain": "app_pages/brain.py", "settings": "app_pages/settings.py"}.get(path, "app_pages/analyst.py")
    st.switch_page(page, query_params=dict(parse_qsl(u.query)))


def pin_button(item: dict) -> None:
    try:
        pinned = any(p["kind"] == item["kind"] and p["id"] == item["id"] for p in api.get(f"/v1/workspaces/{ws}/pins"))
    except APIError:
        return
    if st.button("Pinned" if pinned else "Pin", key="art_pin", type="tertiary",
                 icon=":material/keep:" if pinned else ":material/keep_off:",
                 help="Unpin" if pinned else "Keep it one click away in the sidebar"):
        if pinned:
            kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/pins/{item['kind']}/{item['id']}"), "Unpinned")
        else:
            kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/pins/{item['kind']}/{item['id']}",
                                        {"title": item["title"]}), "Pinned")
        st.session_state.pop("_pins", None)
        st.rerun()


item = artifacts.find(kind, aid) if kind in artifacts.VIEWERS and aid else None
bar = st.columns([1.3, 5, 1.1, 1.3], vertical_alignment="center")
if bar[0].button("Back", icon=":material/arrow_back:", type="tertiary", key="art_back"):
    back()
if not item:
    kit.empty(":material/search_off:", "Not found", "This artifact does not exist in this workspace, or you cannot "
              "open it here. Artifacts belong to the workspace they were made in.")
    st.stop()

label, icon = artifacts.KINDS.get(item["kind"], ("Artifact", ""))
meta = " · ".join(x for x in (label, f"v{item['version']}" if item.get("version") else None,
                              f"updated {kit.when(item.get('updated'))}" if item.get("updated") else None) if x)
with bar[2]:
    pin_button(item)
with bar[3]:
    artifacts.versions(item)
kit.header(item["title"], meta)
a = item.get("asset") or {}
chip = artifacts.HEALTH.get(a.get("health")) if a.get("health") else None
if chip and chip[1] != "ok":
    kit.chip(chip[0], chip[1])
    if a.get("health_reason"):
        st.caption(a["health_reason"])

focus = st.session_state.get("art_focus", False)
if item["kind"] == "analysis" or focus:
    artifacts.VIEWERS[item["kind"]](item)  # an analysis carries its own evidence rail
else:
    main, side = st.columns([2.6, 1], gap="large")
    with main:
        artifacts.VIEWERS[item["kind"]](item)
    with side:
        kit.label("Evidence")
        artifacts._evidence(item)  # noqa: SLF001 - the viewer is the evidence panel's one caller
if item.get("task_id"):
    kit.label("Refine it")
    artifacts.refine(item)
