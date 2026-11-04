# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Reusable pieces: the answer layout, charts, tables, status chips."""

from __future__ import annotations

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


def status_badge(status: str) -> None:
    label, color, icon = STATUS.get(status, (status, "gray", None))
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


def table(t: dict[str, Any]) -> None:
    st.caption(t["title"])
    df = pd.DataFrame(t["rows"], columns=t["columns"])
    cfg = {}
    for col in df.columns:
        if any(k in col for k in ("rate", "effect", "contribution", "share")):
            cfg[col] = st.column_config.NumberColumn(col.replace("_", " ").capitalize(), format="percent")
        elif col in ("prior", "current", "change", "value"):
            cfg[col] = st.column_config.NumberColumn(col.capitalize(), format="compact")
    st.dataframe(df, hide_index=True, width="stretch", column_config=cfg)


def answer(r: dict[str, Any]) -> None:
    """Answer → Evidence → Business implications → Recommended actions → Supporting work."""
    conf = r.get("confidence", {})
    level = conf.get("level", "")
    with st.container(border=True):
        top = st.columns([6, 2], vertical_alignment="center")
        top[0].markdown(f"#### {r['brief'] if r.get('audience') == 'executive' and r.get('brief') else r['answer']}")
        if r["status"] == "needs_input":
            top[1].badge("Needs your input", color="orange", icon=":material/help:")
        elif level == "verified":
            top[1].badge("Verified", color="green", icon=":material/verified:")
        elif level:
            top[1].badge(level.capitalize(), color="orange", icon=":material/warning:")
        if r.get("evidence"):
            st.markdown("**Evidence**")
            st.markdown("\n".join(f"- {e}" for e in r["evidence"]))
        for c in r.get("charts", []):
            chart(c)
        cols = st.columns(2)
        if r.get("implications"):
            with cols[0]:
                st.markdown("**Business implications**")
                st.markdown("\n".join(f"- {e}" for e in r["implications"]))
        if r.get("actions"):
            with cols[1]:
                st.markdown("**Recommended actions**")
                st.markdown("\n".join(f"- {e}" for e in r["actions"]))
        for t in r.get("tables", []):
            table(t)
        if r.get("hypotheses"):
            with st.expander(f"Hypotheses tested ({len(r['hypotheses'])})", icon=":material/science:"):
                icons = {"supported": "✓", "partial": "◐", "refuted": "✕", "untestable": "–"}
                for h in r["hypotheses"]:
                    st.markdown(f"{icons[h['verdict']]} **{h['id'].replace('_', ' ').capitalize()}** · {h['verdict']} · {h['detail']}")
        with st.expander("Supporting work: method, checks and queries", icon=":material/fact_check:"):
            sup = r.get("supporting", {})
            if sup.get("sources"):
                st.markdown("**Sources**")
                for src in sup["sources"]:
                    st.caption(f"{src['dataset']} v{src['version']} · {src['rows']:,} rows · data through {src['data_through']} · "
                               f"sha256 {src['sha256'][:12]}… · loaded {str(src['loaded_at'])[:16]}")
            if sup.get("assumptions"):
                st.markdown("**Assumptions**")
                st.markdown("\n".join(f"- {a}" for a in sup["assumptions"]))
            if sup.get("citations"):
                st.markdown("**Documents consulted**")
                for ci in sup["citations"]:
                    st.caption(f"{ci['title']} · passage {ci['passage']}")
                    st.markdown(f"> {ci['quote']}")
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
