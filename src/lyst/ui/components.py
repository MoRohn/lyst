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


VIEWS = ("Summary", "Detail", "Method")
_OLD_VIEWS = {"Business": "Summary", "Analyst": "Detail", "Engineer": "Method", "Executive": "Summary"}
_CAVEAT = re.compile(r"\b(did not|does not|disagree|differently|no period was named|assumed|filtered|caution|not verified)\b", re.I)


def _default_view(r: dict[str, Any]) -> str:
    pref = st.session_state.get("view_pref")
    pref = _OLD_VIEWS.get(pref, pref)
    if pref in VIEWS:
        return pref
    return "Summary"  # everyone starts at the summary; the depth a person picks is remembered


@st.dialog("Brief", width="large")
def brief(r: dict[str, Any], pdf: Any = None, key: str = "answer") -> None:
    """The two-line version for someone who will read nothing else, with what to do and a file to forward."""
    st.markdown("#### " + md(r.get("brief") or r["answer"]))
    if tl := trust_line(r):
        st.caption(md(tl))
    for c in r.get("charts", [])[:1]:
        chart(c)
    if r.get("actions"):
        st.markdown("**What to do**")
        st.markdown(bullets(r["actions"]))
    if pdf is not None:
        st.download_button("Download the PDF briefing", pdf, file_name=f"lyst-{key}.pdf", mime="application/pdf",
                           icon=":material/download:", type="primary", on_click="ignore", key=f"brief_pdf_{key}")


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


def _join(*parts: Any) -> str:
    return " · ".join(str(p) for p in parts if p not in (None, ""))


def caveats(r: dict[str, Any]) -> list[str]:
    """What a reader must not miss: wrong premises, defaulted periods, filters, disagreements, failed warnings."""
    sup = r.get("supporting") or {}
    out = [a for a in sup.get("assumptions", []) if _CAVEAT.search(a)]
    out += [f"Check not passed: {c.get('name', 'a check')}" + (f" ({c['detail']})" if c.get("detail") else "")
            for c in (r.get("confidence") or {}).get("checks", []) if not c.get("passed", True)]
    return out


def answer(r: dict[str, Any], key: str = "answer", pdf: Any = None) -> None:
    """One result, three depths, no re-run: Summary (answer, evidence, what it means, what to do), Detail (adds
    tables, hypotheses, assumptions and sources), Method (adds plan, checks and every query). Brief opens the
    two-line version with a PDF to forward; an executive request opens on it. Limitations show at every depth."""
    conf = r.get("confidence", {})
    level = conf.get("level", "")
    sup = r.get("supporting", {}) or {}
    with st.container(border=True):
        head = st.empty()
        bar = st.columns([3, 0.9, 2], vertical_alignment="center")
        default = _default_view(r)
        view = bar[2].segmented_control("Depth", VIEWS, default=default, key=f"view_{key}",
                                        label_visibility="collapsed", width="stretch") or default
        st.session_state["view_pref"] = view  # the depth a person picks carries to their next answers
        exec_ = view == "Summary" and r.get("audience") == "executive"  # an executive asked: lead with the brief
        if bar[1].button("Brief", icon=":material/summarize:", type="tertiary", key=f"brief_{key}", width="stretch",
                         help="The two-line version, with what to do and a PDF to forward"):
            brief(r, pdf, key)
        head.markdown("#### " + md(r["brief"] if exec_ and r.get("brief") else r["answer"]))
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
        if not exec_ and r.get("evidence"):
            st.markdown("**Evidence**")
            st.markdown(bullets(r["evidence"]))
        for c in (r.get("charts", [])[:1] if exec_ else r.get("charts", [])):
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
        if view in ("Detail", "Method"):
            for t in r.get("tables", []):
                table(t)
            if r.get("hypotheses"):
                with st.expander(f"Hypotheses tested ({len(r['hypotheses'])})", icon=":material/science:", expanded=True):
                    icons = {"supported": "✓", "partial": "◐", "refuted": "✕", "untestable": "–"}
                    for h in r["hypotheses"]:
                        st.markdown(f"{icons.get(h.get('verdict'), '·')} **{str(h.get('id', '')).replace('_', ' ').capitalize()}**"
                                    f" · {h.get('verdict', '')} · {md(h.get('detail', ''))}")
            if sup.get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown(bullets(sup["assumptions"]))
        with st.expander("Show the method, checks and queries", icon=":material/fact_check:",
                         expanded=view == "Method"):
            if sup.get("sources"):
                st.markdown("**Sources**")
                for src in sup["sources"]:
                    st.caption(md(_join(src.get("dataset"), f"v{src['version']}" if src.get("version") else None,
                                        f"{src['rows']:,} rows" if isinstance(src.get("rows"), int) else None,
                                        f"data through {src['data_through']}" if src.get("data_through") else None,
                                        f"sha256 {str(src['sha256'])[:12]}…" if src.get("sha256") else None,
                                        f"loaded {str(src['loaded_at'])[:16]}" if src.get("loaded_at") else None)))
            if view not in ("Detail", "Method") and sup.get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown(bullets(sup["assumptions"]))
            if sup.get("citations"):
                st.markdown("**Documents consulted**")
                for ci in sup["citations"]:
                    st.caption(md(_join(ci.get("title"), f"passage {ci['passage']}" if ci.get("passage") is not None else None)))
                    if ci.get("quote"):
                        st.markdown("> " + md(ci["quote"]))
            if conf.get("checks"):
                st.markdown(f"**Checks** ({conf.get('basis', '')})")
                st.dataframe(pd.DataFrame(conf["checks"]), hide_index=True, width="stretch",
                             column_config={"passed": st.column_config.CheckboxColumn("Passed")})
            if sup.get("plan"):
                st.markdown("**Plan**  " + " → ".join(f"`{p.get('capability', '?')}`" for p in sup["plan"]))
            for q in sup.get("queries", []):  # scenario answers carry queries without rows, timing or an id
                st.markdown("**" + md(q.get("purpose") or "Query") + "**" + md(" · " + _join(
                    f"{q['rows']} rows" if q.get("rows") is not None else None,
                    f"{q['ms']} ms" if q.get("ms") is not None else None,
                    f"query #{q['query_id']}" if q.get("query_id") else None) if any(
                    q.get(k) is not None for k in ("rows", "ms", "query_id")) else ""))
                if q.get("sql"):
                    st.code(q["sql"], language="sql")
            if sup.get("reproduce"):
                st.caption(sup["reproduce"] + f" Narrative written by: {sup.get('narrative_by')}.")
