# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Questions about an answer (how it was figured, where it came from, what could change it, can it be repeated),
how sensitive each dataset is, and what each level may be used for. Reads the session on every call."""

from __future__ import annotations

import re

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

ASK = {"how": "How was this figured?", "sources": "Where is it from?", "change": "What could change it?",
       "reproduce": "Can it be repeated?"}
LEVELS = {"public": "Public", "internal": "Internal", "confidential": "Confidential", "restricted": "Restricted"}
ROLES = {"viewer": "Everyone", "editor": "Editors and above", "admin": "Admins and above", "owner": "Owners only"}


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("admin", "owner")


def _lines(detail) -> list[str]:
    """Readable lines from an explanation's detail, whatever its shape."""
    if isinstance(detail, str):
        return [detail]
    if isinstance(detail, list):
        out = []
        for d in detail:
            if isinstance(d, str):
                out.append(d)
            elif isinstance(d, dict):
                if d.get("figure"):
                    steps = [s.get("description") or s.get("capability") for s in d.get("steps") or []]
                    out.append(f"{d['figure']}: " + "; ".join(x for x in steps if x))
                elif d.get("query"):
                    out.append(f"{d['query']}: {d.get('outcome')} ({d.get('rows_then')} rows then, {d.get('rows_now')} now)")
                elif d.get("capability"):
                    out.append(f"{d['capability'].replace('_', ' ')}: {d.get('why') or ''}")
        return out
    if isinstance(detail, dict):
        ds = detail.get("datasets") or []
        out = [f"{d.get('label')}: {d.get('rows', 0):,} rows, loaded {str(d.get('loaded_at'))[:16]}" for d in ds]
        cur = detail.get("currency") or {}
        if cur.get("changed"):
            out.append("Changed since: " + "; ".join(map(str, cur["changed"])))
        out += [f"Warning: {w}" for w in detail.get("warnings") or []]
        return out
    return []


STEP = {"metric_value": "Value", "metric_series": "Trend", "driver_analysis": "Drivers", "anomaly_scan": "Unusual moves",
        "forecast": "Forecast", "scenario": "What-if", "breakdown": "Breakdown"}

CSS = """<style>
/* About this answer: each question answered as things to look at, not a paragraph of identifiers */
.lyst-ex-def {display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; padding: .85rem 1rem; margin: .25rem 0 .9rem;
  border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-md); background: var(--lyst-raised);}
.lyst-ex-def b {font-weight: var(--lyst-weight-medium);}
.lyst-ex-def code {font-family: inherit; font-size: 1rem; color: var(--lyst-text); background: none; padding: 0;}
.lyst-ex-def span {color: var(--lyst-text-muted); font-size: .82rem;}
.lyst-ex-grid {display: grid; grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); gap: .6rem; margin-bottom: .6rem;}
.lyst-ex-tile {padding: .75rem .85rem; border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-md);
  background: var(--lyst-raised);}
.lyst-ex-tile b {display: block; font-size: 1.25rem; font-weight: var(--lyst-weight-strong); letter-spacing: -.01em;
  font-variant-numeric: tabular-nums;}
.lyst-ex-tile span {display: block; margin-top: .2rem; color: var(--lyst-text-muted); font-size: .8rem; line-height: 1.35;}
.lyst-ex-tile em {font-style: normal; color: var(--lyst-text);}
.lyst-ex-big {display: flex; align-items: center; gap: .75rem; padding: .9rem 1rem; margin-bottom: .6rem;
  border-radius: var(--lyst-radius-md); background: color-mix(in srgb, var(--lyst-good) 9%, transparent);}
.lyst-ex-big.warn {background: color-mix(in srgb, var(--lyst-warn) 12%, transparent);}
.lyst-ex-big b {font-size: 1.05rem; font-weight: var(--lyst-weight-medium);}
.lyst-ex-big i {flex: 0 0 .7rem; height: .7rem; border-radius: 50%; background: var(--lyst-good);}
.lyst-ex-big.warn i {background: var(--lyst-warn);}
.lyst-ex-list {margin: 0; padding: 0; list-style: none;}
.lyst-ex-list li {display: flex; gap: .65rem; padding: .6rem 0; border-bottom: 1px solid var(--lyst-border); line-height: 1.45;}
.lyst-ex-list li:last-child {border-bottom: 0;}
.lyst-ex-list li::before {content: ""; flex: 0 0 .45rem; height: .45rem; margin-top: .5rem; border-radius: 50%;
  background: var(--lyst-accent);}
.lyst-ex-list li.warn::before {background: var(--lyst-warn);}
</style>"""


def readable(expr: str) -> str:
    """A metric's SQL as people say it: CAST(SUM("operating_income") AS DOUBLE) / NULLIF(SUM("revenue"), 0) reads
    'operating income ÷ revenue'. Anything it can't simplify is left as written."""
    e = re.sub(r"CAST\((.*?) AS \w+\)", r"\1", expr or "")
    e = re.sub(r"NULLIF\((.*?),\s*0\)", r"\1", e)
    e = re.sub(r'(SUM|AVG|COUNT|MIN|MAX)\("?([a-z0-9_]+)"?\)', lambda m: m.group(2).replace("_", " "), e, flags=re.I)
    e = e.replace('"', "").replace(" / ", " ÷ ").replace(" * ", " × ")
    return e[:1].upper() + e[1:] if "(" not in e else expr


def _titles() -> dict[str, str]:
    api, ws, _ = _ctx()
    try:
        return {d["name"]: d.get("title") or d["name"] for d in api.get(f"/v1/workspaces/{ws}/datasets")}
    except APIError:
        return {}


def _dataset(label: str, titles: dict[str, str]) -> str:
    """'pnl v1' as people know it: 'Monthly P&L, version 1'."""
    name, _, ver = str(label).rpartition(" v")
    return f"{titles[name]}, version {ver}" if name in titles and ver.isdigit() else str(label)


def _how(detail: list) -> None:
    figs = [d for d in detail if isinstance(d, dict) and d.get("figure")]
    defs = {(x["metric"], x.get("version")): x for d in figs for x in d.get("definition") or []}
    for (metric, ver), x in defs.items():
        st.html(f'<div class="lyst-ex-def"><b>{kit._esc(metric.replace("_", " ").capitalize())}</b>'
                f'<code>= {kit._esc(readable(x.get("expression") or ""))}</code><span>approved definition v{ver}</span></div>')
    tiles = []
    for d in figs:
        steps = list(dict.fromkeys(STEP.get(x.get("capability"), str(x.get("capability", "")).replace("_", " ").capitalize())
                                   for x in d.get("steps") or []))
        n = len(d.get("queries") or [])
        how = (f"<em>{kit._esc(' · '.join(steps))}</em><br>{n} quer{'y' if n == 1 else 'ies'}" if steps
               else "Worked out from the figures beside it")
        tiles.append(f'<div class="lyst-ex-tile"><b>{kit._esc(d["figure"])}</b><span>{how}</span></div>')
    if tiles:
        st.html('<div class="lyst-ex-grid">' + "".join(tiles) + "</div>")
    data = sorted({x for d in figs for x in d.get("data") or []})
    if data:
        titles = _titles()
        st.caption("From " + ", ".join(_dataset(x, titles) for x in data) + ".")
    sql = list(dict.fromkeys(q["sql"] for d in figs for q in d.get("queries") or [] if q.get("sql")))
    if sql:
        with st.expander(f"The queries · {len(sql)}", icon=":material/code:"):
            for q in sql:
                st.code(q, language="sql", wrap_lines=True)


def _sources(detail: dict) -> None:
    titles = _titles()
    tiles = [f'<div class="lyst-ex-tile"><b>{kit._esc(titles.get(d.get("dataset"), d.get("label") or d.get("dataset") or ""))}</b>'
             f'<span>Version {d.get("version")} · {d.get("rows", 0):,} rows · loaded {kit._esc(kit.when(d.get("loaded_at")))}'
             + (f'<br><em>{kit._esc(LEVELS.get(d.get("classification"), d.get("classification") or ""))}</em>'
                if d.get("classification") else "") + "</span></div>"
             for d in detail.get("datasets") or []]
    tiles += [f'<div class="lyst-ex-tile"><b>{kit._esc(x.get("label") or "")}</b><span>The file it was loaded from</span></div>'
              for x in detail.get("sources") or []]
    tiles += [f'<div class="lyst-ex-tile"><b>{kit._esc(x.get("label") or x.get("title") or "")}</b><span>A document passage</span></div>'
              for x in detail.get("passages") or [] if isinstance(x, dict)]
    if tiles:
        st.html('<div class="lyst-ex-grid">' + "".join(tiles) + "</div>")


def _change(detail: dict) -> None:
    cur = detail.get("currency") or {}
    changed = cur.get("changed") or []
    st.html(f'<div class="lyst-ex-big{" warn" if changed else ""}"><i></i><b>'
            + ("The data has changed since this answer: " + kit._esc("; ".join(map(str, changed))) if changed
               else "The data is the same as when Lyst answered") + "</b></div>")
    items = [("warn", w) for w in detail.get("warnings") or []] + [("", a) for a in detail.get("assumptions") or []]
    if items:
        kit.label("It rests on")
        st.html('<ul class="lyst-ex-list">' + "".join(f'<li class="{c}">{kit._esc(t)}</li>' for c, t in items) + "</ul>")


def _reproduce(r: dict) -> None:
    runs = [d for d in r.get("detail") or [] if isinstance(d, dict) and d.get("query")]
    same = sum(d.get("outcome") == "same" for d in runs)
    ok = bool(r.get("reproducible")) and same == len(runs)
    st.html(f'<div class="lyst-ex-big{"" if ok else " warn"}"><i></i><b>'
            + (f"Run again now, {same} of {len(runs)} queries return the same rows" if runs else kit._esc(md(r.get("short") or "")))
            + "</b></div>")
    differ = [d for d in runs if d.get("outcome") != "same"]
    if differ:
        st.html('<ul class="lyst-ex-list">' + "".join(
            f'<li class="warn">A query now returns {d.get("rows_now")} rows; it returned {d.get("rows_then")}</li>' for d in differ)
            + "</ul>")


@st.dialog("About this answer", width="large")
def explain(task: str, q: str) -> None:
    api, _, _ = _ctx()
    st.html(CSS)
    pick = st.segmented_control("Question", list(ASK), default=q, format_func=ASK.get, label_visibility="collapsed",
                                key=f"ex_q_{task}") or q
    r = kit.guarded(lambda: api.get(f"/v1/tasks/{task}/explain", q=pick))
    if not r:
        return
    detail = r.get("detail")
    if pick == "how" and isinstance(detail, list):
        _how(detail)
    elif pick == "sources" and isinstance(detail, dict):
        _sources(detail)
    elif pick == "change" and isinstance(detail, dict):
        _change(detail)
    elif pick == "reproduce":
        _reproduce(r)
    else:  # an answer shaped some other way: its own words, the lines a click away
        st.markdown(md(r.get("short") or ""))
        lines = _lines(detail)
        if lines:
            with st.expander("Details", icon=":material/list:"):
                st.markdown("\n".join(f"- {md(x)}" for x in lines[:20]))


def ask_row(task: str) -> None:
    """Under an answer: the questions people ask about a number before they repeat it."""
    pick = st.pills("Ask about this answer", list(ASK), format_func=ASK.get, label_visibility="collapsed",
                    key=f"ex_{task}")
    if pick:
        st.session_state.pop(f"ex_{task}", None)
        explain(task, pick)


def classification(name: str, current: str | None) -> None:
    """Dataset Overview: how sensitive it is and what that allows; admins change it with a reason."""
    api, ws, admin = _ctx()
    try:
        pol = (api.get(f"/v1/workspaces/{ws}/data-policy").get("spec")) or {}
    except APIError:
        pol = {}
    lvl = (current or "internal").lower()
    who = ROLES.get((pol.get("read_min_role") or {}).get(lvl, "viewer"), "Everyone")
    rank = list(LEVELS)
    ext = rank.index(lvl) <= rank.index(pol.get("external_model_max", "confidential")) if lvl in rank else True
    exp = rank.index(lvl) <= rank.index(pol.get("export_max", "confidential")) if lvl in rank else True
    kit.label("Sensitivity")
    st.caption(md(f"{LEVELS.get(lvl, lvl)}: readable by {who.lower()}; "
                  + ("may" if ext else "may not") + " be sent to an external model; "
                  + ("may" if exp else "may not") + " be exported."))
    if admin:
        with st.popover("Change sensitivity", icon=":material/shield:"):
            new = st.selectbox("Level", list(LEVELS), index=rank.index(lvl) if lvl in rank else 1, format_func=LEVELS.get,
                               key=f"cl_{name}")
            why = st.text_input("Reason", placeholder="Contains salary data", key=f"clr_{name}")
            if st.button("Save", type="primary", key=f"cls_{name}", disabled=not why.strip() or new == lvl):
                kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/datasets/{name}/classification",
                                            {"level": new, "tags": [], "reason": why.strip()}), "Saved")


def policy_summary() -> str:
    """The data policy in one sentence: who reads restricted and confidential data, and what may leave."""
    api, ws, _admin = _ctx()
    try:
        spec = (api.get(f"/v1/workspaces/{ws}/data-policy").get("spec") or {})
    except APIError:
        return ""
    reads = spec.get("read_min_role") or {}
    return (f"Restricted data: {ROLES.get(reads.get('restricted', 'admin'), '').lower()}. Confidential data: "
            f"{ROLES.get(reads.get('confidential', 'viewer'), '').lower()}. Models receive data up to "
            f"{LEVELS.get(spec.get('external_model_max', 'confidential'), '').lower()}; exports up to "
            f"{LEVELS.get(spec.get('export_max', 'confidential'), '').lower()}.")


def policy_section() -> None:
    """Settings › Workspace: who may read each sensitivity level, and what may leave the workspace."""
    api, ws, admin = _ctx()
    try:
        p = api.get(f"/v1/workspaces/{ws}/data-policy")
    except APIError:
        return
    spec = p.get("spec") or {}
    kit.label("Data policy")
    if not admin:
        st.caption(md("; ".join(f"{LEVELS[k]} data: {ROLES.get(v, v).lower()}" for k, v in (spec.get("read_min_role") or {}).items()
                                if k in LEVELS) + "."))
        return
    reads = {}
    cols = st.columns(len(LEVELS))
    for col, (k, label) in zip(cols, LEVELS.items(), strict=True):
        cur = (spec.get("read_min_role") or {}).get(k, "viewer")
        reads[k] = col.selectbox(f"{label} data is read by", list(ROLES), index=list(ROLES).index(cur), format_func=ROLES.get,
                                 key=f"dp_{k}")
    c = st.columns(2)
    ext = c[0].selectbox("Send to an external model data up to", list(LEVELS), format_func=LEVELS.get,
                         index=list(LEVELS).index(spec.get("external_model_max", "confidential")), key="dp_ext")
    exp = c[1].selectbox("Allow exports of data up to", list(LEVELS), format_func=LEVELS.get,
                         index=list(LEVELS).index(spec.get("export_max", "confidential")), key="dp_exp")
    new = {"read_min_role": reads, "external_model_max": ext, "export_max": exp}
    if new != {k: spec.get(k) for k in new} and st.button("Save policy", type="primary", key="dp_save"):
        kit.guarded(lambda: api.put(f"/v1/workspaces/{ws}/data-policy", new), "Policy saved")
