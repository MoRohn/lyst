# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Reusable pieces: the answer layout, charts, tables, status chips."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

STATUS = {
    "queued": ("Queued", "gray", ":material/schedule:"),
    "running": ("Running", "blue", ":material/progress_activity:"),
    "paused": ("Paused", "orange", ":material/pause:"),
    "awaiting_approval": ("Needs approval", "orange", ":material/approval:"),
    "done": ("Done", "green", ":material/check:"),
    "failed": ("Failed", "red", ":material/error:"),
    "cancelled": ("Cancelled", "gray", ":material/block:"),
}


def md(text: object) -> str:
    """Text for st.markdown: Streamlit renders $...$ as LaTeX, so "$25.41M ... $25.10M" became math.
    Every model- or data-derived string shown as markdown goes through this."""
    return str(text).replace("\\", "\\\\").replace("$", "\\$")


def bullets(items: list) -> str:
    return "\n".join(f"- {md(e)}" for e in items)


OUTCOME = {
    "needs_input": ("Needs your input", "orange", ":material/help:"),
    "unverified": ("Not verified", "red", ":material/gpp_maybe:"),
}


def task_state(status: str, outcome: str | None = None) -> tuple[str, str, str | None]:
    """One label for a task everywhere: a finished run that needs input or failed verification is not 'Done'."""
    if status == "done" and outcome in OUTCOME:
        return OUTCOME[outcome]
    return STATUS.get(status, (status, "gray", None))


def status_badge(status: str, outcome: str | None = None) -> None:
    label, color, icon = task_state(status, outcome)
    st.badge(label, color=color, icon=icon)


def _fmt_axis(fmt: str) -> dict[str, Any]:
    return {"percent": {"tickformat": ".0%"}, "currency": {"tickprefix": "$", "tickformat": "~s"}}.get(fmt, {})


def chart(c: dict[str, Any]) -> None:
    fig = go.Figure()
    if c["type"] == "forecast":
        fig.add_scatter(x=c.get("history_x", []), y=c.get("history_y", []), mode="lines", name="Actual")
        fig.add_scatter(x=c["x"] + c["x"][::-1], y=c["hi"] + c["lo"][::-1], fill="toself", mode="lines",
                        line={"width": 0}, opacity=0.2, name="Interval", hoverinfo="skip")
        fig.add_scatter(x=c["x"], y=c["y"], mode="lines", line={"dash": "dot"}, name="Forecast")
    elif c["type"] == "bar":
        fig.add_bar(x=c["x"], y=c["y"], name=c["title"])
    else:
        fig.add_scatter(x=c["x"], y=c["y"], mode="lines+markers", marker={"size": 4}, name=c["title"])
    fig.update_layout(title=None, height=300, margin={"l": 8, "r": 8, "t": 8, "b": 8}, showlegend=c["type"] == "forecast",
                      yaxis=_fmt_axis(c.get("format", "number")), hovermode="x unified")
    st.caption(c["title"])
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def heading(col: str) -> str:
    """'prior_rate' -> 'Prior rate', 'region' -> 'Region': column names in words, acronyms kept."""
    text = str(col).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def table(t: dict[str, Any]) -> None:
    st.caption(t["title"])
    df = pd.DataFrame(t["rows"], columns=t["columns"])
    for col in df.columns:  # -0.0 rendered as "-0%": round tiny values to a plain zero
        if pd.api.types.is_float_dtype(df[col]):
            df[col] = df[col].mask(df[col].abs() < 5e-7, 0.0)
    cfg = {}
    for col in df.columns:
        if any(k in col for k in ("rate", "effect", "contribution", "share")):
            cfg[col] = st.column_config.NumberColumn(heading(col), format="percent")
        elif col in ("prior", "current", "change", "value"):
            cfg[col] = st.column_config.NumberColumn(heading(col), format="compact")
        else:
            cfg[col] = st.column_config.Column(heading(col))
    st.dataframe(df, hide_index=True, width="stretch", column_config=cfg)


VIEWS = ("Business", "Analyst", "Engineer", "Executive")
_CAVEAT = re.compile(r"\b(did not|does not|disagree|differently|no period was named|assumed|filtered|caution|not verified)\b", re.I)


def _default_view(r: dict[str, Any]) -> str:
    if r.get("audience") == "executive":
        return "Executive"
    if st.session_state.get("view_pref") in VIEWS:
        return st.session_state["view_pref"]
    return "Business"  # everyone starts at the business depth; the depth a person picks is remembered


def trust_line(r: dict[str, Any]) -> str | None:
    """What 'Verified' means for this answer, in one line a business reader can repeat."""
    conf = r.get("confidence") or {}
    checks = conf.get("checks") or []
    passed = sum(1 for c in checks if c.get("passed"))
    if r.get("status") == "needs_input":
        return "Lyst needs one more detail before it can answer; see below."
    if conf.get("level") == "verified":
        return (f"All {len(checks)} checks passed: every figure was recomputed independently from approved data."
                if checks else "Every figure was recomputed independently from approved data.")
    if checks:
        return f"{passed} of {len(checks)} checks passed. Read the notes below before relying on this."
    return None


def caveats(r: dict[str, Any]) -> list[str]:
    """What a reader must not miss: wrong premises, defaulted periods, filters, disagreements, failed warnings."""
    sup = r.get("supporting") or {}
    out = [a for a in sup.get("assumptions", []) if _CAVEAT.search(a)]
    out += [f"Check not passed: {c['name']} ({c['detail']})" for c in (r.get("confidence") or {}).get("checks", [])
            if not c["passed"]]
    return out


def answer(r: dict[str, Any], key: str = "answer") -> None:
    """One result, four depths, no re-run: Business (answer, evidence, implications, actions), Analyst (adds tables,
    hypotheses, assumptions and sources), Engineer (adds plan, checks and every query), Executive (the brief and
    what to do). Limitations are visible at every depth."""
    conf = r.get("confidence", {})
    level = conf.get("level", "")
    sup = r.get("supporting", {}) or {}
    with st.container(border=True):
        head = st.empty()
        bar = st.columns([3, 2], vertical_alignment="center")
        default = _default_view(r)
        view = bar[1].segmented_control("View", VIEWS, default=default, key=f"view_{key}",
                                        label_visibility="collapsed", width="stretch") or default
        if not (view == "Executive" and r.get("audience") == "executive"):
            st.session_state["view_pref"] = view  # the depth a person picks carries to their next answers
        head.markdown("#### " + md(r["brief"] if view == "Executive" and r.get("brief") else r["answer"]))
        if r["status"] == "needs_input":
            bar[0].badge("Needs your input", color="orange", icon=":material/help:")
        elif level == "verified":
            bar[0].badge("Verified", color="green", icon=":material/verified:")
        elif level:
            bar[0].badge(level.capitalize(), color="orange", icon=":material/warning:")
        if tl := trust_line(r):
            st.caption(md(tl))
        wf = sup.get("workflow") or {}
        if wf.get("used"):
            st.caption(f":material/bolt: Reused the checked analysis “{md(wf['used']['title'])}”, verified "
                       f"{wf['used']['verified_runs']}× before, so this took one pass instead of being worked out again.")
        elif (wf.get("compiled") or {}).get("created"):
            st.caption(":material/library_add_check: Saved as a repeatable analysis: rerun it for any period, or have "
                       "it repeat on its own (Repeat it, below).")
        cav = caveats(r)
        if cav:
            st.warning(bullets(cav), icon=":material/info:")
        if view != "Executive" and r.get("evidence"):
            st.markdown("**Evidence**")
            st.markdown(bullets(r["evidence"]))
        for c in (r.get("charts", [])[:1] if view == "Executive" else r.get("charts", [])):
            chart(c)
        cols = st.columns(2)
        if r.get("implications"):
            with cols[0]:
                st.markdown("**What it means**")
                st.markdown(bullets(r["implications"]))
        if r.get("actions"):
            with cols[1]:
                st.markdown("**What to do**")
                st.markdown(bullets(r["actions"]))
        if view in ("Analyst", "Engineer"):
            for t in r.get("tables", []):
                table(t)
            if r.get("hypotheses"):
                with st.expander(f"Hypotheses tested ({len(r['hypotheses'])})", icon=":material/science:", expanded=True):
                    icons = {"supported": "✓", "partial": "◐", "refuted": "✕", "untestable": "–"}
                    for h in r["hypotheses"]:
                        st.markdown(f"{icons[h['verdict']]} **{h['id'].replace('_', ' ').capitalize()}** · {h['verdict']} · "
                                    f"{md(h['detail'])}")
            if sup.get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown(bullets(sup["assumptions"]))
        with st.expander("Show the method, checks and queries", icon=":material/fact_check:",
                         expanded=view == "Engineer"):
            if sup.get("sources"):
                st.markdown("**Sources**")
                for src in sup["sources"]:
                    st.caption(f"{src['dataset']} v{src['version']} · {src['rows']:,} rows · data through {src['data_through']} · "
                               f"sha256 {src['sha256'][:12]}… · loaded {str(src['loaded_at'])[:16]}")
            if view not in ("Analyst", "Engineer") and sup.get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown(bullets(sup["assumptions"]))
            if sup.get("citations"):
                st.markdown("**Documents consulted**")
                for ci in sup["citations"]:
                    st.caption(f"{ci['title']} · passage {ci['passage']}")
                    st.markdown("> " + md(ci["quote"]))
            if conf.get("checks"):
                st.markdown(f"**Checks** ({conf.get('basis', '')})")
                st.dataframe(pd.DataFrame(conf["checks"]), hide_index=True, width="stretch",
                             column_config={"passed": st.column_config.CheckboxColumn("Passed")})
            if sup.get("plan"):
                st.markdown("**Plan**  " + " → ".join(f"`{p['capability']}`" for p in sup["plan"]))
            for q in sup.get("queries", []):
                st.markdown(f"**{q['purpose']}** · {q['rows']} rows · {q['ms']} ms · query #{q['query_id']}")
                st.code(q["sql"], language="sql")
            if sup.get("reproduce"):
                st.caption(sup["reproduce"] + f" Narrative written by: {sup.get('narrative_by')}.")
