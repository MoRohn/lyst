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


def _mode() -> str:
    try:
        return st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        return "light"


def _alt(c: dict[str, Any]) -> str:
    """A text equivalent of the chart for screen readers: what it shows, its range and its last value."""
    ys = [v for v in (c.get("y") or []) if isinstance(v, int | float)]
    xs = c.get("x") or []
    if not ys:
        return str(c.get("title") or "Chart")
    fmt = c.get("format", "number")
    show = (lambda v: f"{v:.1%}") if fmt == "percent" else (lambda v: f"{v:,.4g}")
    kind = {"forecast": "Forecast", "bar": "Bar chart"}.get(c.get("type"), "Line chart")
    return (f"{kind}: {c.get('title', '')}. {len(ys)} points from {xs[0] if xs else ''} to {xs[-1] if xs else ''}; "
            f"lowest {show(min(ys))}, highest {show(max(ys))}, last {show(ys[-1])}.")


def chart(c: dict[str, Any]) -> None:
    """An analytical chart in Lyst's theme: the accent for the measure, a muted line for history, the forecast as a
    dotted line inside a shaded interval, and the latest point marked so the eye lands on what changed."""
    from lyst.ui import tokens  # noqa: PLC0415

    mode = _mode()
    col = tokens.colors(mode)
    accent = tokens.series(mode)[0]
    fig = go.Figure()
    if c["type"] == "forecast":
        fig.add_scatter(x=c.get("history_x", []), y=c.get("history_y", []), mode="lines", name="Actual",
                        line={"color": col["text-muted"], "width": 1.6})
        fig.add_scatter(x=c["x"] + c["x"][::-1], y=c["hi"] + c["lo"][::-1], fill="toself", mode="lines",
                        line={"width": 0}, fillcolor=accent, opacity=0.16, name="Interval", hoverinfo="skip")
        fig.add_scatter(x=c["x"], y=c["y"], mode="lines", line={"dash": "dot", "color": accent, "width": 2},
                        name="Forecast")
    elif c["type"] == "bar":
        fig.add_bar(x=c["x"], y=c["y"], name=c["title"], marker={"color": accent})
    else:
        fig.add_scatter(x=c["x"], y=c["y"], mode="lines", line={"color": accent, "width": 2}, name=c["title"])
        if c.get("x") and c.get("y"):  # the latest observation, marked
            fig.add_scatter(x=[c["x"][-1]], y=[c["y"][-1]], mode="markers", showlegend=False, hoverinfo="skip",
                            marker={"size": 7, "color": accent, "line": {"color": col["bg"], "width": 2}})
    fig.update_layout(**tokens.plotly_layout(mode, fmt=c.get("format", "number")), height=280,
                      showlegend=c["type"] == "forecast", hovermode="x unified")
    st.caption(c["title"])
    st.plotly_chart(fig, width="stretch", theme=None, config={"displayModeBar": False}, alt=_alt(c))


def breakdown_chart(r: dict[str, Any]) -> dict[str, Any] | None:
    """A breakdown answer (a table of one category column and one number) as a bar chart, so the Summary shows
    the split at a glance instead of only naming the largest part. Only when the answer has no chart of its own."""
    if r.get("charts"):
        return None
    for t in r.get("tables") or []:
        cols, rows = t.get("columns") or [], t.get("rows") or []
        if len(cols) == 2 and 2 <= len(rows) <= 24 and all(
                isinstance(row[1], int | float) and not isinstance(row[0], int | float) for row in rows):
            fmt = "percent" if any(k in cols[1] for k in ("rate", "share", "margin", "ratio")) else (
                "currency" if "$" in str(r.get("answer", "")) else "number")
            return {"type": "bar", "title": t.get("title") or f"{heading(cols[1])} by {heading(cols[0]).lower()}",
                    "x": [str(row[0]) for row in rows], "y": [float(row[1]) for row in rows], "format": fmt}
    return None


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
    # Missing values read as an em dash, not "None"; long tables scroll inside a fixed height instead of pushing the
    # answer's actions off screen. Sorting, search, column resizing, copy and CSV export are the grid's own.
    height = "auto" if len(df) <= 12 else 38 + 35 * 12
    st.dataframe(df, hide_index=True, width="stretch", column_config=cfg, placeholder="—", height=height)


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


RULE_TONE = {"passed": "good", "enforced": "good", "failed": "bad", "not_automated": "warn", "not_applicable": "neutral"}


def evidence_rail(r: dict[str, Any]) -> None:
    """What stands behind the answer, beside it: the checks, the data it came from, the expertise applied and the
    assumptions made. Everything here is the engine's record for this result; nothing is inferred by the UI."""
    from html import escape  # noqa: PLC0415

    from lyst.ui import visuals  # noqa: PLC0415

    conf = r.get("confidence") or {}
    sup = r.get("supporting") or {}
    checks = conf.get("checks") or []
    parts: list[str] = []

    def section(title: str, body: str) -> None:
        parts.append(f'<div class="lyst-rail-section"><div class="lyst-card-kicker">{escape(title)}</div>{body}</div>')

    if checks:
        ok = sum(1 for c in checks if c.get("passed"))
        tone = "good" if ok == len(checks) else ("warn" if ok else "bad")
        section("Checks", visuals.meter(ok / len(checks), label="Checks passed", tone=tone, text=f"{ok} of {len(checks)} passed"))
    srcs = list({(s.get("dataset"), s.get("version")): s for s in sup.get("sources") or []}.values())
    if srcs:
        rows = "".join(
            f'<div class="lyst-rail-item"><b>{escape(str(s.get("dataset", "")))}</b> '
            + escape(_join(f"v{s['version']}" if s.get("version") else None,
                           f"{s['rows']:,} rows" if isinstance(s.get("rows"), int) else None,
                           f"through {s['data_through']}" if s.get("data_through") else None)) + "</div>"
            for s in srcs[:4])
        section("Data", f'<div class="lyst-card-meta">{rows}</div>')
    ex = sup.get("expertise") or {}
    if ex:
        disc = ex.get("disciplines") or []
        names = ", ".join(str(d.get("name", d.get("id", ""))) if isinstance(d, dict) else str(d) for d in disc[:4])
        rules = ex.get("rules") or []
        counts: dict[str, int] = {}
        for rule in rules:
            counts[rule.get("status", "")] = counts.get(rule.get("status", ""), 0) + 1
        body = f'<div class="lyst-card-meta">{escape(names or str(ex.get("primary") or ""))}</div>'
        if rules:
            body += visuals.stacked([(f"{k.replace('_', ' ')} {v}", v, RULE_TONE.get(k, "neutral"))
                                     for k, v in counts.items() if v],
                                    label="Expertise rules for this answer", height=6)
        if ex.get("note"):
            body += f'<div class="lyst-card-meta">{escape(str(ex["note"]))}</div>'
        section("Expertise applied", body)
    assumptions = sup.get("assumptions") or []
    if assumptions:
        section(f"Assumptions · {len(assumptions)}", '<div class="lyst-card-meta">'
                + "<br>".join(escape(str(a)) for a in assumptions[:3]) + ("<br>…" if len(assumptions) > 3 else "")
                + "</div>")
    q = sup.get("queries") or []
    if q or sup.get("plan"):
        section("Method", f'<div class="lyst-card-meta">{len(sup.get("plan") or [])} steps · {len(q)} governed '
                          f'quer{"y" if len(q) == 1 else "ies"}. Open “Show the method” for each.</div>')
    if parts:
        st.html('<div class="lyst-rail">' + "".join(parts) + "</div>")


def made_here(r: dict[str, Any], key: str) -> None:
    """The artifacts this answer produced, as previews that open in the viewer: each chart, on its own."""
    tid = key.removeprefix("av_")
    if not tid.startswith("t_") or not r.get("charts"):
        return
    from lyst.ui import artifacts  # noqa: PLC0415

    items = [{"kind": "chart", "id": f"{tid}:{i}", "task_id": tid, "title": c.get("title") or f"Chart {i + 1}",
              "updated": None} for i, c in enumerate(r["charts"][:4])]
    st.html('<div class="lyst-card-kicker" style="margin-top:.6rem">Made in this answer</div>')
    artifacts.grid(items, key=f"made_{tid}", per_row=4)


def answer(r: dict[str, Any], key: str = "answer", pdf: Any = None) -> None:
    """One result, three depths, no re-run: Summary (answer, evidence, what it means, what to do), Detail (adds
    tables, hypotheses, assumptions and sources), Method (adds plan, checks and every query). Brief opens the
    two-line version with a PDF to forward; an executive request opens on it. Limitations show at every depth."""
    conf = r.get("confidence", {})
    level = conf.get("level", "")
    sup = r.get("supporting", {}) or {}
    with st.container(border=True):
        head = st.empty()
        bar = st.columns([2.4, 0.9, 0.9, 1, 2], vertical_alignment="center")
        default = _default_view(r)
        view = bar[4].segmented_control("Depth", VIEWS, default=default, key=f"view_{key}",
                                        label_visibility="collapsed", width="stretch") or default
        st.session_state["view_pref"] = view  # the depth a person picks carries to their next answers
        exec_ = view == "Summary" and r.get("audience") == "executive"  # an executive asked: lead with the brief
        if bar[1].button("Brief", icon=":material/summarize:", type="tertiary", key=f"brief_{key}", width="stretch",
                         help="The two-line version, with what to do and a PDF to forward"):
            brief(r, pdf, key)
        fk = f"focus_{key}"
        bar[2].button("Evidence" if st.session_state.get(fk) else "Focus", type="tertiary", key=f"focusbtn_{key}",
                      icon=":material/view_sidebar:" if st.session_state.get(fk) else ":material/fullscreen:",
                      width="stretch", help="Show or hide the evidence beside the answer",
                      on_click=lambda: st.session_state.update({fk: not st.session_state.get(fk)}))
        if key.removeprefix("av_").startswith("t_"):  # what Lyst can do next with this answer, from the engine
            from lyst.ui import actions  # noqa: PLC0415

            with bar[3]:
                actions.menu("task", key.removeprefix("av_"), page="home", label="More", exclude=("task.open",))
        head.markdown("#### " + md(r["brief"] if exec_ and r.get("brief") else r["answer"]))
        if r["status"] == "needs_input":
            bar[0].badge("Needs your input", color="orange", icon=":material/help:")
        elif level == "verified":
            bar[0].badge("Verified", color="green", icon=":material/verified:")
        elif level:
            bar[0].badge(level.capitalize(), color="orange", icon=":material/warning:")
        focus = bool(st.session_state.get(f"focus_{key}"))
        # the canvas: the answer on the left, its evidence beside it; Focus gives the answer the whole width
        main, rail = (st.container(), None) if focus else st.columns([2.35, 1], gap="large")
        with main:
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
            # one chart in the flow of the finding; every chart is also an artifact below, to open on its own
            for c in r.get("charts", [])[:1]:
                chart(c)
            if view == "Summary" and (bc := breakdown_chart(r)):
                chart(bc)
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
        if rail is not None:
            with rail:
                evidence_rail(r)
        made_here(r, key)
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
