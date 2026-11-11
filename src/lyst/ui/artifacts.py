# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Artifacts: what Lyst makes, as objects people find, open, refine and come back to.

The application organises and operates on artifacts; it does not become one. Everything here reads the engine's
durable records, never Streamlit session state:

* **analyses**: finished tasks and their verified results (``GET /v1/workspaces/{ws}/tasks``, ``/v1/tasks/{id}``);
* **charts**: each chart inside an analysis, addressed as ``<task id>:<n>``;
* **assets**: the registry of deliverables with ids, versions, lineage, validation and health
  (``GET /v1/workspaces/{ws}/assets``): dashboards, saved analyses (workflows), PDF briefings, Markdown briefings,
  slides and workbooks.

:func:`catalog` lists them as one model, :func:`tile` draws one with a preview thumbnail made from its own content,
:func:`open_artifact` goes to the immersive viewer (the hidden ``artifact`` page, ``?kind=&id=``), and
:data:`VIEWERS` is the registry the viewer uses so each kind opens with its own native interactions.
"""

from __future__ import annotations

import base64
import io
import re
from html import escape

import streamlit as st

from lyst.ui import kit, tokens, visuals
from lyst.ui.client import APIError, client

KINDS: dict[str, tuple[str, str]] = {
    "analysis": ("Analysis", ":material/insights:"),
    "chart": ("Chart", ":material/show_chart:"),
    "dashboard": ("Dashboard", ":material/dashboard:"),
    "workflow": ("Saved analysis", ":material/bolt:"),
    "report": ("PDF briefing", ":material/picture_as_pdf:"),
    "briefing": ("Briefing", ":material/article:"),
    "presentation": ("Slides", ":material/slideshow:"),
    "workbook": ("Workbook", ":material/table_view:"),
}
HEALTH = {"current": ("Current", "ok"), "stale": ("Newer data", "warn"), "invalidated": ("Definition changed", "warn"),
          "broken": ("Broken", "bad")}
EXT = {"report": ("pdf", "application/pdf"), "briefing": ("md", "text/markdown"),
       "presentation": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
       "workbook": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


# ------------------------------------------------------------------------------------------------ catalog

def _ws() -> str:
    return st.session_state.workspace


def result(task_id: str) -> dict | None:
    """A finished task's result. Finished results never change, so they are kept per session by id."""
    memo = st.session_state.setdefault("_art_results", {})
    if task_id in memo:
        return memo[task_id]
    try:
        t = client().get(f"/v1/tasks/{task_id}")
    except APIError:
        return None
    r = t.get("result") if t.get("status") == "done" else None
    if r is not None:
        if len(memo) > 200:  # bounded: oldest first out
            memo.pop(next(iter(memo)))
        memo[task_id] = {**r, "_task": {k: t.get(k) for k in ("id", "title", "status", "outcome", "created_at",
                                                                "updated_at", "workspace_id")}}
    return memo.get(task_id)


def catalog(*, limit: int = 60) -> list[dict]:
    """Every artifact in this workspace as one list, newest first: analyses and the registry's assets."""
    api, ws = client(), _ws()
    items: list[dict] = []
    try:
        for t in api.get(f"/v1/workspaces/{ws}/tasks"):
            if t["status"] == "done" and t.get("kind", "analysis") in ("analysis", "scheduled", "workflow"):
                items.append({"kind": "analysis", "id": t["id"], "title": t.get("title") or "Analysis",
                              "updated": t.get("updated_at") or t.get("created_at"), "task_id": t["id"],
                              "status": ("Needs input", "warn") if t.get("outcome") == "needs_input" else
                              (("Not verified", "bad") if t.get("outcome") == "unverified" else None)})
    except APIError:
        pass
    try:
        for a in api.get(f"/v1/workspaces/{ws}/assets"):
            if a.get("status") == "retired":
                continue
            items.append({"kind": a["kind"] if a["kind"] in KINDS else "report", "id": a["id"], "title": a["title"],
                          "updated": a.get("updated_at"), "version": a.get("version"), "task_id": a.get("task_id"),
                          "ref_id": a.get("ref_id"), "asset": a,
                          "status": HEALTH.get(a.get("health")) if a.get("health") != "current" else
                          (("Verified", "verified") if a.get("validation") == "verified" else None)})
    except APIError:
        pass
    items.sort(key=lambda x: str(x.get("updated") or ""), reverse=True)
    return items[:limit]


# --------------------------------------------------------------------------------------------- thumbnails

def _svg(body: str, mode: str, w: int = 240, h: int = 132) -> str:
    c = tokens.colors(mode)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" rx="10" '
           f'fill="{c["surface"]}"/>{body}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _line(ys: list[float], mode: str, *, x0=16, y0=18, w=208, h=96, bars=False) -> str:
    c = tokens.colors(mode)
    acc = tokens.series(mode)[0]
    ys = [float(v) for v in ys if isinstance(v, int | float)]
    if len(ys) < 2:
        return ""
    lo, hi = (0.0 if bars else min(ys)), max(ys)
    span = (hi - lo) or 1.0
    if bars:
        bw = w / len(ys)
        return "".join(f'<rect x="{x0 + i * bw + bw * .15:.1f}" y="{y0 + h * (1 - (v - lo) / span):.1f}" '
                       f'width="{bw * .7:.1f}" height="{h * (v - lo) / span:.1f}" rx="2" fill="{acc}" '
                       f'fill-opacity="{1 - i * .08 if i < 6 else .5:.2f}"/>' for i, v in enumerate(ys[:12]))
    step = w / (len(ys) - 1)
    pts = " ".join(f"{x0 + i * step:.1f},{y0 + h * (1 - (v - lo) / span):.1f}" for i, v in enumerate(ys))
    grid = "".join(f'<line x1="{x0}" x2="{x0 + w}" y1="{y0 + h * k / 3:.1f}" y2="{y0 + h * k / 3:.1f}" '
                   f'stroke="{c["border"]}" stroke-width="1"/>' for k in range(4))
    return (grid + f'<polyline points="{pts}" fill="none" stroke="{acc}" stroke-width="2.5" stroke-linejoin="round" '
            f'stroke-linecap="round"/>')


def _glyph(kind: str, mode: str) -> str:
    """A drawing of what the kind of artifact looks like, for assets whose content is a file."""
    c = tokens.colors(mode)
    acc, mut, raised = tokens.series(mode)[0], c["border"], c["raised"]
    if kind == "dashboard":
        return (f'<rect x="16" y="16" width="100" height="44" rx="6" fill="{raised}"/>'
                f'<rect x="124" y="16" width="100" height="44" rx="6" fill="{raised}"/>'
                f'<rect x="16" y="68" width="208" height="48" rx="6" fill="{raised}"/>'
                f'<polyline points="28,104 64,90 100,96 136,80 172,86 212,74" fill="none" stroke="{acc}" stroke-width="2.5"/>'
                f'<rect x="28" y="28" width="40" height="8" rx="3" fill="{mut}"/><rect x="136" y="28" width="52" height="8" '
                f'rx="3" fill="{mut}"/>')
    if kind == "workflow":
        nodes = "".join(f'<rect x="{16 + i * 74}" y="52" width="60" height="28" rx="8" fill="{raised}" stroke="{acc}" '
                        f'stroke-opacity="{.4 + i * .3:.1f}"/>' for i in range(3))
        return nodes + "".join(f'<line x1="{76 + i * 74}" x2="{90 + i * 74}" y1="66" y2="66" stroke="{acc}" '
                               f'stroke-width="2"/>' for i in range(2))
    if kind == "workbook":
        cells = "".join(f'<rect x="{20 + col * 52}" y="{22 + row * 22}" width="48" height="18" rx="2" '
                        f'fill="{acc if row == 0 else raised}" fill-opacity="{.85 if row == 0 else 1}"/>'
                        for row in range(4) for col in range(4))
        return cells
    if kind == "presentation":
        return (f'<rect x="40" y="18" width="160" height="90" rx="6" fill="{raised}"/>'
                f'<rect x="56" y="34" width="80" height="10" rx="3" fill="{acc}"/>'
                + "".join(f'<rect x="56" y="{54 + k * 12}" width="{110 - k * 20}" height="6" rx="3" fill="{mut}"/>'
                          for k in range(3)) + f'<rect x="96" y="114" width="48" height="6" rx="3" fill="{mut}"/>')
    # report / briefing: a page with a title and lines
    return (f'<rect x="64" y="12" width="112" height="108" rx="6" fill="{raised}"/>'
            f'<rect x="76" y="26" width="64" height="9" rx="3" fill="{acc}"/>'
            + "".join(f'<rect x="76" y="{44 + k * 11}" width="{88 - (k % 3) * 14}" height="5" rx="2.5" fill="{mut}"/>'
                      for k in range(6)))


def thumbnail(item: dict, mode: str) -> str:
    """A preview drawn from the artifact itself: an analysis or chart from its own numbers, a file from its kind."""
    kind = item["kind"]
    if kind in ("analysis", "chart"):
        r = result(item["task_id"]) or {}
        charts = r.get("charts") or []
        if kind == "chart":
            charts = charts[int(item["id"].split(":")[1]):][:1]
        if charts:
            ch = charts[0]
            ys = ch.get("history_y", []) + ch.get("y", []) if ch.get("type") == "forecast" else ch.get("y", [])
            return _svg(_line(ys, mode, bars=ch.get("type") == "bar"), mode)
        for t in r.get("tables") or []:
            nums = [row[-1] for row in t.get("rows", []) if isinstance(row[-1], int | float)]
            if len(nums) >= 2:
                return _svg(_line(nums[:12], mode, bars=True), mode)
        return _svg(_glyph("report", mode), mode)
    return _svg(_glyph(kind, mode), mode)


# -------------------------------------------------------------------------------------------- tiles

def open_artifact(item: dict) -> None:
    """Go to the immersive viewer, remembering where the person came from so Back returns there."""
    try:
        from urllib.parse import urlencode, urlparse  # noqa: PLC0415

        here = urlparse(st.context.url).path + "?" + urlencode(st.query_params.to_dict())
    except Exception:  # noqa: BLE001 - outside a browser session
        here = ""
    st.session_state["_artifact_from"] = here
    if item["kind"] == "analysis":  # an analysis is a conversation: it opens where it can be continued
        st.session_state.task_id = item["id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": item["id"]})
    st.switch_page("app_pages/artifact.py", query_params={"kind": item["kind"], "id": item["id"]})


def tile(item: dict, *, key: str) -> None:
    mode = visuals.mode()
    label, icon = KINDS.get(item["kind"], (item["kind"].capitalize(), ":material/draft:"))
    with kit.card(f"art_{key}"):
        st.html(f'<img class="lyst-thumb" src="{thumbnail(item, mode)}" alt="Preview of {escape(item["title"])}">'
                f'<div class="lyst-card-kicker" style="margin-top:.55rem">{escape(label)}'
                + (f' · v{item["version"]}' if item.get("version") else "") + "</div>"
                f'<div class="lyst-card-title lyst-clamp">{escape(item["title"])}</div>'
                f'<div class="lyst-card-meta">{escape(kit.when(item.get("updated")))}</div>')
        c = st.columns([1.2, 1.6], vertical_alignment="center")
        if c[0].button("Open", key=f"open_{key}", type="tertiary", icon=":material/open_in_full:"):
            open_artifact(item)
        if item.get("status"):
            kit.chip(item["status"][0], item["status"][1], c[1])


def grid(items: list[dict], *, key: str, per_row: int = 3) -> None:
    for start in range(0, len(items), per_row):
        cols = st.columns(per_row)
        for col, it in zip(cols, items[start:start + per_row], strict=False):
            with col:
                tile(it, key=f"{key}_{it['kind']}_{it['id']}".replace(":", "_"))


def shelf(*, title: str = "Your work", n: int = 6, key: str = "shelf", exclude: tuple[str, ...] = ()) -> None:
    """The latest artifacts, as previews: the way back into any piece of work."""
    items = [i for i in catalog(limit=60) if i["kind"] not in exclude][:n]
    if not items:
        return
    kit.label(title)
    grid(items, key=key)


def gallery(*, key: str = "gal", kinds: tuple[str, ...] | None = None) -> None:
    """Every artifact, searchable and filterable by kind."""
    items = [i for i in catalog(limit=200) if not kinds or i["kind"] in kinds]
    if not items:
        kit.empty(":material/collections:", "Nothing made yet", "Answers, dashboards, briefings and saved analyses "
                  "Lyst makes appear here, each with a preview, to open, refine and share.")
        return
    q = st.text_input("Find", placeholder="Find an artifact", label_visibility="collapsed", key=f"{key}_q",
                      icon=":material/search:")
    present = [k for k in KINDS if any(i["kind"] == k for i in items)]
    pick = st.pills("Kind", present, selection_mode="multi", format_func=lambda k: KINDS[k][0],
                        label_visibility="collapsed", key=f"{key}_kinds") or []
    shown = [i for i in items if (not q or q.lower() in i["title"].lower()) and (not pick or i["kind"] in pick)]
    st.caption(f"{len(shown)} of {len(items)}")
    grid(shown, key=key)


# ------------------------------------------------------------------------------------------- viewers

def find(kind: str, aid: str) -> dict | None:
    """One artifact by its stable id, read from the engine (so a reload or a shared link opens the same thing)."""
    api, ws = client(), _ws()
    if kind in ("analysis", "chart"):
        tid = aid.split(":")[0]
        r = result(tid)
        if not r:
            return None
        if kind == "chart":
            n = int(aid.split(":")[1]) if ":" in aid else 0
            ch = (r.get("charts") or [None] * (n + 1))[n:n + 1]
            if not ch or not ch[0]:
                return None
            return {"kind": "chart", "id": aid, "task_id": tid, "title": ch[0].get("title") or "Chart",
                    "updated": r["_task"].get("updated_at"), "chart": ch[0], "result": r}
        return {"kind": "analysis", "id": tid, "task_id": tid, "title": r["_task"].get("title") or "Analysis",
                "updated": r["_task"].get("updated_at"), "result": r}
    try:
        a = api.get(f"/v1/workspaces/{ws}/assets/{aid}")
    except APIError:
        return None
    return {"kind": a["kind"], "id": a["id"], "title": a["title"], "updated": a.get("updated_at"),
            "version": a.get("version"), "task_id": a.get("task_id"), "ref_id": a.get("ref_id"), "asset": a}


def refine(item: dict) -> None:
    """Ask Lyst to change this artifact in words; the new version keeps what it came from (follow-up lineage)."""
    tid = item.get("task_id")
    if not tid:
        return
    with st.form(f"refine_{item['id']}", clear_on_submit=True, border=False):
        c = st.columns([6, 1.2], vertical_alignment="bottom")
        text = c[0].text_input("Refine", placeholder="Change it: “break that down by region”, “compare it to last "
                               "year”, “make this a dashboard”", label_visibility="collapsed")
        if c[1].form_submit_button("Refine", type="primary", icon=":material/auto_fix_high:", width="stretch") \
                and text.strip():
            out = kit.guarded(lambda: client().post(f"/v1/tasks/{tid}/follow-up", {"text": text.strip(),
                                                                                    "timezone": st.context.timezone}))
            new = (out or {}).get("task_id") or (out or {}).get("id")
            if out and not new and out.get("message"):
                st.info(out["message"], icon=":material/lightbulb:")  # the engine says what it can change
            if new:  # the refined version runs as a new analysis that records what it came from; watch it there
                st.session_state.task_id = new
                st.switch_page("app_pages/analyst.py", query_params={"task": new})


def _evidence(item: dict) -> None:
    from lyst.ui.components import evidence_rail  # noqa: PLC0415

    r = item.get("result") or (result(item["task_id"]) if item.get("task_id") else None)
    if r:
        evidence_rail(r)
    a = item.get("asset")
    if a:
        deps = a.get("depends") or {}
        st.html(visuals.flow([("Data", ", ".join(f"{k} v{v}" for k, v in (deps.get("datasets") or {}).items()) or "—", ""),
                              ("Definitions", ", ".join(f"{k} v{v}" for k, v in (deps.get("metrics") or {}).items())
                               or "—", ""),
                              (KINDS.get(a["kind"], ("Artifact", ""))[0], a["title"], f"v{a.get('version', 1)}")],
                             label=f"What {a['title']} is built on"))


def view_analysis(item: dict) -> None:
    from lyst.ui.components import answer  # noqa: PLC0415

    answer(item["result"], key=f"av_{item['id']}")


def view_chart(item: dict) -> None:
    from lyst.ui.components import chart, table  # noqa: PLC0415

    chart({**item["chart"], "height": 460})
    for t in (item["result"].get("tables") or [])[:1]:
        table(t)
    st.caption(f"From the analysis “{item['result']['_task'].get('title')}”.")


def view_dashboard(item: dict, *, key: str = "", actions: bool = True) -> None:
    """The dashboard itself, from its own published extracts: a KPI tile per metric (latest value, change, recent
    history), then each metric's trend beside its split for the latest period, with a period range to filter. The same
    view opens in the viewer and in place under the answer that built it, so nobody has to run the app to see it."""
    import pandas as pd  # noqa: PLC0415

    from lyst.ui.components import chart  # noqa: PLC0415
    from lyst.ui.scenarios import fmt  # noqa: PLC0415

    api, ws = client(), _ws()
    did = item.get("ref_id") or item["id"]
    k = f"{key}{did}"
    man = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/dashboards/{did}"))
    if not man:
        return

    def extract(name: str | None) -> pd.DataFrame | None:
        if not name:
            return None
        try:
            raw = api.http.get(f"/v1/workspaces/{ws}/dashboards/{did}/files/{name}", headers=api._h())
            raw.raise_for_status()
            return pd.read_parquet(io.BytesIO(raw.content))
        except Exception:  # noqa: BLE001 - an extract that cannot be read leaves that panel out, not the dashboard
            return None

    series = []
    for m in man.get("metrics") or []:
        s = extract(m.get("series_file"))
        if s is not None and len(s):
            series.append((m, s.sort_values("period"), extract(m.get("breakdown_file"))))
    if not series:
        st.caption("This dashboard has no published series yet.")
        return
    dim = man.get("dimension") or ""
    tiles = st.columns(min(4, len(series)))
    for i, (m, s, _) in enumerate(series[:4]):
        vals = [float(v) for v in s["value"] if pd.notna(v)]
        if not vals:
            continue
        fmt_ = m.get("format", "number")
        change, good = "", None
        if len(vals) > 1:
            d = vals[-1] - vals[-2]
            change = f"{d * 100:+.1f} pts" if fmt_ == "percent" else ("+" if d >= 0 else "") + fmt(d, fmt_)
            good = (d >= 0) == bool(m.get("higher_is_better", True)) if d else None
        with tiles[i % len(tiles)], kit.card(f"dbt_{k}_{i}"):
            st.html(visuals.metric_tile({"title": m["title"], "value_text": fmt(vals[-1], fmt_),
                                         "change_text": change, "good": good, "history": vals[-12:],
                                         "grain": man.get("grain") or "month"}))
    periods = sorted({str(p)[:10] for _, s, _ in series for p in s["period"]})
    if len(periods) > 2:
        lo, hi = st.select_slider("Period", periods, value=(periods[max(0, len(periods) - 13)], periods[-1]),
                                  key=f"dbr_{k}")
    else:
        lo, hi = periods[0], periods[-1]
    for m, s, b in series:
        s = s[(s["period"].astype(str).str[:10] >= lo) & (s["period"].astype(str).str[:10] <= hi)]
        line = {"type": "line", "title": m["title"], "x": [str(p)[:10] for p in s["period"]],
                "y": [float(v) for v in s["value"]], "format": m.get("format", "number")}
        split = None
        if b is not None and dim in b.columns and len(b):
            last = b[b["period"].astype(str).str[:10] <= hi]["period"].max() if "period" in b.columns else None
            lb = (b[b["period"] == last] if last is not None else b).sort_values("value", ascending=False)
            lb = lb[lb["value"].notna()]
            if len(lb) >= 2:
                split = {"type": "bar", "title": f"{m['title']} by {dim.replace('_', ' ')}"
                         + (f", {str(last)[:7]}" if last is not None else ""),
                         "x": [str(x) for x in lb[dim]], "y": [float(v) for v in lb["value"]],
                         "format": m.get("format", "number")}
        if split:
            c = st.columns([3, 2], gap="medium")
            with c[0]:
                chart(line)
            with c[1]:
                chart(split)
        else:
            chart(line)
    checks = man.get("checks") or []
    if checks:
        st.caption("Checks: " + " · ".join(("✓ " if c["passed"] else "✕ ") + c["name"].replace("_", " ") for c in checks))
    if not actions:
        return
    c = st.columns([1.4, 1.6, 4])
    if c[0].button("Refresh now", icon=":material/refresh:", key=f"dbref_{k}"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/dashboards/{did}/refresh"), "Refreshed")
    c[1].download_button("Download app", lambda: api.http.get(f"/v1/workspaces/{ws}/dashboards/{did}/bundle",
                                                               headers=api._h()).content,
                         file_name=f"{did}.zip", mime="application/zip", on_click="ignore", icon=":material/download:",
                         key=f"dbdl_{k}", help="The app and its data, to run on another machine")


def opened(ref: dict | None) -> dict | None:
    """The viewer item for an answer's ``open`` pointer ({"kind", "id", "title", "asset_id"?}), or None when the
    artifact is not one Lyst can show here or can no longer be read."""
    if not ref or ref.get("kind") not in VIEWERS:
        return None
    if ref["kind"] == "dashboard":  # read by its own id; its registry entry (when there is one) opens the full viewer
        aid = ref.get("asset_id")
        if not aid:
            try:
                aid = next((a["id"] for a in client().get(f"/v1/workspaces/{_ws()}/assets", kind="dashboard")
                            if a.get("ref_id") == ref["id"]), None)
            except (APIError, TypeError):
                aid = None
        return {"kind": "dashboard", "id": aid or ref["id"], "ref_id": ref["id"], "asset_id": aid,
                "title": ref.get("title") or "Dashboard", "task_id": None}
    return find(ref["kind"], ref["id"])


def show_inline(ref: dict | None, *, key: str) -> bool:
    """Open the artifact an answer made or asked for, in place: the thing itself, not a description of it, with a way
    to open it full size. Returns False (and shows nothing) when there is nothing Lyst can show."""
    if ref and ref.get("kind") == "quality":
        return quality_inline(str(ref.get("id") or ""), key=key)
    item = opened(ref)
    if not item:
        return False
    label, icon = KINDS.get(item["kind"], ("Artifact", ":material/draft:"))
    with st.container(border=True, key=f"lyst_open_{key}"):
        top = st.columns([6, 1.4], vertical_alignment="center")
        top[0].html(f'<div class="lyst-card-kicker">{escape(label)}</div>'
                    f'<div class="lyst-card-title">{escape(item["title"])}</div>')
        full = item["kind"] != "dashboard" or item.get("asset_id")  # the viewer opens registered artifacts
        if full and top[1].button("Open full view", icon=":material/open_in_full:", type="tertiary",
                                  key=f"openfull_{key}", width="stretch"):
            open_artifact(item)
        if item["kind"] == "dashboard":
            view_dashboard(item, key=f"{key}_", actions=True)
        else:
            VIEWERS[item["kind"]](item)
    return True


def quality_inline(dataset: str, *, key: str) -> bool:
    """An answer that profiled a dataset opens its flagged rows as the review table, in place: each row to accept, fix
    or exclude, and Let Lyst resolve, with a way to open the dataset on Data."""
    from lyst.ui import quality  # noqa: PLC0415

    if not dataset:
        return False
    with st.container(border=True, key=f"lyst_open_{key}"):
        top = st.columns([6, 1.4], vertical_alignment="center")
        top[0].html('<div class="lyst-card-kicker">Data quality review</div>'
                    f'<div class="lyst-card-title">Flagged rows in {escape(dataset)}</div>')
        if top[1].button("Open on Data", icon=":material/open_in_new:", type="tertiary", key=f"openq_{key}", width="stretch"):
            st.switch_page("app_pages/data.py", query_params={"dataset": dataset, "review": "quality"})
        quality.review(dataset, key=f"ans_{key}", compact=True)
    return True


def view_workflow(item: dict) -> None:
    """A saved analysis: how it runs, how often it was verified, and a run for any period."""
    api, ws = client(), _ws()
    w = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/workflows/{item.get('ref_id') or item['id']}"))
    if not w:
        return
    d = w.get("definition") or {}
    st.html(visuals.flow([(f"Step {i + 1}", s.get("capability", "").replace("_", " ").capitalize(), "")
                          for i, s in enumerate(d.get("steps") or [])][:6], label="How this analysis runs")
            + visuals.stats([("verified runs", f"{w.get('verified_runs', 0)}", None), ("reused", f"{w.get('runs', 0)}", None),
                             ("last used", kit.when(w.get("last_used_at")) or "not yet", None)], compact=True))
    if d.get("purpose"):
        st.caption(d["purpose"])
    grain = (d.get("inputs", {}).get("period") or {}).get("grain")
    c = st.columns([2, 1.2], vertical_alignment="bottom")
    period = c[0].text_input("For which period", placeholder=f"last {grain}" if grain else "", disabled=not grain,
                             key=f"wfp_{w['id']}", help="For example Q4 2026. Empty means the last complete one.")
    if c[1].button("Run it", type="primary", icon=":material/play_arrow:", width="stretch", key=f"wfr_{w['id']}",
                   disabled=w.get("status") != "active"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/workflows/{w['id']}/run", {"period": period.strip() or None}))
        if r:
            st.session_state.task_id = r["task_id"]
            st.switch_page("app_pages/analyst.py", query_params={"task": r["task_id"]})


def view_file(item: dict) -> None:
    """A briefing, deck, workbook or PDF. All four are built from one answer, so a PDF, deck or workbook opens on
    that answer as the answer page draws it (charts live, in this theme); the file's own pages, slides or sheets
    follow, and the file to keep. A briefing is its own Markdown, charts embedded."""
    api = client()
    a = item.get("asset") or {}
    ext, mime = EXT.get(item["kind"], ("bin", "application/octet-stream"))
    data = api.http.get(f"/v1/artifacts/{a.get('ref_id')}", headers=api._h()).content if a.get("ref_id") else b""
    if data:
        label = KINDS[item["kind"]][0]
        label = label if label[:2].isupper() else label[0].lower() + label[1:]  # "PDF briefing" keeps its PDF
        st.download_button(f"Download the {label}", data,
                           file_name=f"{item['title'][:60]}.{ext}", mime=mime, on_click="ignore", type="primary",
                           icon=":material/download:", key=f"fdl_{item['id']}")
    from lyst.ui.components import md  # noqa: PLC0415

    text = data.decode("utf-8", "replace") if item["kind"] == "briefing" and data else ""
    r = None if text else (result(item["task_id"]) if item.get("task_id") else None)
    own = {"report": "Pages", "presentation": "Slides", "workbook": "Sheets"}.get(item["kind"]) if data else None
    tabs = st.tabs(["Preview", own]) if own and (text or r) else [st.container()]
    if text:
        with tabs[0], kit.card(f"doc_{item['id']}"):
            st.markdown(md(_inline(text)))
    elif r:  # the answer the file was built from, as the answer page draws it: live charts, in this theme
        from lyst.ui.components import answer  # noqa: PLC0415

        with tabs[0]:
            answer(r, key=f"fp_{item['id']}")
    elif not own:
        st.html(f'<img class="lyst-thumb" style="max-width:420px" src="{thumbnail(item, visuals.mode())}" alt="">')
    if own:
        with tabs[-1]:
            {"report": _pages, "presentation": _slide_cards, "workbook": _sheets}[item["kind"]](item, data)


def _inline(text: str) -> str:
    """The briefing read inside the viewer: the page already shows its title, so the first heading goes, and its
    sections step down to the card's heading size. A chart's data table (folded in the file under <details>, which
    the viewer would print as tags) stays out: the chart is drawn above it and its rows are under Sheets or the file."""
    text = re.sub(r"<details>.*?</details>\n?", "", text, flags=re.S)
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    lines = [ln for ln in lines if not ln.startswith("*Lyst · ")]  # the stamp: the header says kind and when
    return "\n".join("####" + ln[2:] if ln.startswith("## ") else "#####" + ln[3:] if ln.startswith("### ") else ln
                     for ln in lines).strip()


def _pages(item: dict, data: bytes) -> None:
    pages = _pdf_pages(data)
    for i, text in enumerate(pages):
        with kit.card(f"pg_{item['id']}_{i}"):
            st.html(f'<div class="lyst-card-kicker">Page {i + 1} of {len(pages)}</div>'
                    f'<div class="lyst-page">{escape(text)}</div>')


def _slide_cards(item: dict, data: bytes) -> None:
    slides = _slides(data)
    cols = st.columns(2)
    for i, (title, lines) in enumerate(slides):
        with cols[i % 2], kit.card(f"sl_{item['id']}_{i}"):
            st.html(f'<div class="lyst-slide"><div class="lyst-card-kicker">Slide {i + 1}</div>'
                    f'<div class="lyst-card-title">{escape(title)}</div>'
                    + "".join(f'<div class="lyst-card-meta">{escape(t)}</div>' for t in lines[:6]) + "</div>")


def _sheets(item: dict, data: bytes) -> None:
    """Each table sheet under its own title, with the column names the sheet writes under that title. The Summary
    sheet is the answer, which Preview already shows."""
    sheets = [sh for sh in sheet_tables(data) if sh["name"] != "Summary"]
    if not sheets:
        return
    for tab, sh in zip(st.tabs([sh["title"] for sh in sheets]), sheets, strict=True):
        with tab:
            if sh["note"]:
                st.caption(sh["note"])
            st.dataframe(sh["frame"], hide_index=True, width="stretch", placeholder="—",
                         column_config=sh["config"])


def sheet_tables(data: bytes) -> list[dict]:
    """A Lyst workbook's sheets as tables. Each sheet starts with a title (row 1), maybe a note (row 2), a blank row,
    then the header row (deliverables._sheet_head), so the first non-empty row after the title is the header."""
    import pandas as pd  # noqa: PLC0415
    from openpyxl import load_workbook  # noqa: PLC0415

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True)
    except Exception:  # noqa: BLE001 - an unreadable file still downloads
        return []
    out = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows()]
        vals = [[c.value for c in r] for r in rows]
        if not vals:
            continue
        title = str(vals[0][0] or ws.title)
        note = str(vals[1][0]) if len(vals) > 1 and vals[1] and vals[1][0] is not None else ""
        head = next((i for i in range(1, len(vals)) if sum(v is not None for v in vals[i]) >= 2), None)
        if head is None:
            continue
        width = max((j + 1 for r in vals[head:] for j, v in enumerate(r) if v is not None), default=0)
        names, seen = [], set()
        for j in range(width):
            n = str(vals[head][j]) if j < len(vals[head]) and vals[head][j] is not None else f"Column {j + 1}"
            while n in seen:
                n += " "
            seen.add(n)
            names.append(n)
        body = [(r + [None] * width)[:width] for r in vals[head + 1:] if any(v is not None for v in r)]
        frame = pd.DataFrame(body, columns=names)
        config = {}
        fmt_row = rows[head + 1] if head + 1 < len(rows) else []
        for j, n in enumerate(names):
            f = getattr(fmt_row[j], "number_format", "") if j < len(fmt_row) else ""
            if "%" in (f or "") and pd.api.types.is_numeric_dtype(frame[n]):
                frame[n] = frame[n] * 100
                config[n] = st.column_config.NumberColumn(n, format="%.1f%%")
            elif pd.api.types.is_datetime64_any_dtype(frame[n]):
                monthly = bool(frame[n].dropna().dt.day.eq(1).all())
                config[n] = st.column_config.DateColumn(n, format="MMM YYYY" if monthly else "D MMM YYYY")
        out.append({"name": ws.title, "title": title, "note": note, "frame": frame, "config": config})
    return out


def _pdf_pages(data: bytes) -> list[str]:
    """The PDF's text, page by page (the file itself is the download below)."""
    try:
        from pypdf import PdfReader  # noqa: PLC0415

        return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages][:20]
    except Exception:  # noqa: BLE001 - an unreadable file still downloads
        return []


def _slides(data: bytes) -> list[tuple[str, list[str]]]:
    try:
        from pptx import Presentation  # noqa: PLC0415
    except ImportError:
        return []
    out = []
    for slide in Presentation(io.BytesIO(data)).slides:
        texts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.text_frame.text]
        title = slide.shapes.title.text if slide.shapes.title is not None else (texts[0] if texts else "")
        out.append((title, [t for t in texts if t != title]))
    return out


VIEWERS = {"analysis": view_analysis, "chart": view_chart, "dashboard": view_dashboard, "workflow": view_workflow,
           "report": view_file, "briefing": view_file, "presentation": view_file, "workbook": view_file}


def versions(item: dict) -> None:
    a = item.get("asset") or {}
    vs = a.get("versions") or []
    if not vs:
        return
    with st.popover(f"Versions · {len(vs)}", icon=":material/history:"):
        for v in vs:
            st.markdown(f"**v{v['version']}** · {kit.when(v.get('created_at'))} · {v.get('validation') or ''}")
            if v.get("note"):
                st.caption(v["note"])


CSS = """
/* a briefing's charts are SVG drawn as in the file (dark text on paper): on a paper surface in dark mode too */
[class*="st-key-lyst_card_doc_"] img {background: #fff; border-radius: var(--lyst-radius-md); padding: .5rem; max-width: 100%;}
img.lyst-thumb {width: 100%; aspect-ratio: 240 / 132; border-radius: var(--lyst-radius-md); display: block;}
.lyst-page {white-space: pre-wrap; font-size: var(--lyst-size-sm); line-height: var(--lyst-leading-body);
             max-width: 70ch;}
.lyst-slide {aspect-ratio: 16 / 9; overflow: hidden; display: grid; align-content: start; gap: .3rem;}
.lyst-clamp {display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
             min-height: 2.6em;}
"""
