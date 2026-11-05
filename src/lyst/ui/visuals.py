# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Small, data-dense visuals drawn as SVG images and plain HTML/CSS: sparklines, proportion bars, value distributions, lineage flows
and stat strips. They render in one pass with the page (no chart library, no iframe, no extra rerun), take their
colours from the design tokens for the active theme, and each carries a text equivalent (``role="img"`` with an
``aria-label``) so a screen reader gets the same facts the eye does.

Every visual is a function returning an HTML string, so pages compose them inside cards; :func:`show` renders one.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from html import escape
from typing import Any

import streamlit as st

from lyst.ui import tokens


def mode() -> str:
    try:
        return st.context.theme.type or "light"
    except Exception:  # noqa: BLE001 - outside a browser session
        return "light"


def show(html: str) -> None:
    st.html(html)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and v == v else None  # noqa: PLR0124 - NaN check


def spark(values: Sequence[Any], *, label: str, tone: str = "accent", width: int = 220, height: int = 44,
          band: bool = True) -> str:
    """A sparkline: the series as a line, a faint area under it, and the latest point marked."""
    ys = [v for v in (_num(x) for x in values) if v is not None]
    c = tokens.colors(mode())
    color = c.get(tone, c["accent"])
    if len(ys) < 2:
        return f'<div class="lyst-viz-empty" role="img" aria-label="{escape(label)}: not enough points">—</div>'
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or abs(hi) or 1.0
    pad = 4
    step = (width - 2 * pad) / (len(ys) - 1)
    pts = [(pad + i * step, pad + (height - 2 * pad) * (1 - (y - lo) / span)) for i, y in enumerate(ys)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pad},{height - pad} {line} {pts[-1][0]:.1f},{height - pad}"
    lx, ly = pts[-1]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
           + (f'<polygon points="{area}" fill="{color}" fill-opacity=".10"/>' if band else "")
           + f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.8" '
             f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
           + f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="{color}" stroke="{c["bg"]}" stroke-width="1.5"/>'
           + "</svg>")
    # st.html sanitises inline <svg> away, so the drawing travels as an image; alt carries the same facts
    data = base64.b64encode(svg.encode()).decode()
    return (f'<img class="lyst-spark" src="data:image/svg+xml;base64,{data}" alt="{escape(label)}" '
            f'style="width:100%;height:{height}px">')


def stacked(parts: Sequence[tuple[str, float, str]], *, label: str, height: int = 8) -> str:
    """A proportion bar: (name, share, tone) parts, with a legend underneath."""
    c = tokens.colors(mode())
    total = sum(max(p[1], 0) for p in parts) or 1.0
    segs = "".join(f'<span style="flex:{max(s, 0) / total:.4f};background:{c.get(t, t)}" title="{escape(n)}"></span>'
                   for n, s, t in parts if s > 0)
    legend = "".join(f'<span class="lyst-legend"><i style="background:{c.get(t, t)}"></i>{escape(n)}</span>'
                     for n, s, t in parts if s > 0)
    desc = ", ".join(f"{n} {s / total:.0%}" for n, s, _ in parts if s > 0)
    return (f'<div role="img" aria-label="{escape(label)}: {escape(desc)}">'
            f'<div class="lyst-stack" style="height:{height}px">{segs}</div>'
            f'<div class="lyst-legends">{legend}</div></div>')


def meter(share: float, *, label: str, tone: str = "accent", text: str | None = None) -> str:
    """A thin horizontal meter (0..1), for completeness, coverage or a top value's share."""
    c = tokens.colors(mode())
    share = min(max(share, 0.0), 1.0)
    return (f'<div class="lyst-meter" role="img" aria-label="{escape(label)} {share:.0%}">'
            f'<span style="width:{share * 100:.1f}%;background:{c.get(tone, tone)}"></span></div>'
            + (f'<div class="lyst-meter-text">{escape(text)}</div>' if text else ""))


def bars(items: Sequence[tuple[str, float]], *, label: str, tone: str = "accent", fmt: str = "{:.0%}") -> str:
    """Labelled horizontal bars: top values of a column, a breakdown, a share of total."""
    c = tokens.colors(mode())
    top = max((v for _, v in items), default=0) or 1.0
    rows = "".join(
        f'<div class="lyst-bar"><span class="lyst-bar-label">{escape(str(n))}</span>'
        f'<span class="lyst-bar-track"><span style="width:{max(v, 0) / top * 100:.1f}%;background:{c.get(tone, tone)}">'
        f'</span></span><span class="lyst-bar-value lyst-num">{escape(fmt.format(v))}</span></div>'
        for n, v in items)
    desc = ", ".join(f"{n} {fmt.format(v)}" for n, v in items)
    return f'<div class="lyst-bars" role="img" aria-label="{escape(label)}: {escape(desc)}">{rows}</div>'


def flow(steps: Sequence[tuple[str, str, str]], *, label: str) -> str:
    """A left-to-right lineage: (kind, name, detail) boxes joined by arrows; wraps on narrow screens."""
    boxes = []
    for i, (kind, name, detail) in enumerate(steps):
        boxes.append(f'<div class="lyst-flow-node"><div class="lyst-flow-kind">{escape(kind)}</div>'
                     f'<div class="lyst-flow-name">{escape(name)}</div>'
                     + (f'<div class="lyst-flow-detail">{escape(detail)}</div>' if detail else "") + "</div>")
        if i < len(steps) - 1:
            boxes.append('<div class="lyst-flow-arrow" aria-hidden="true">→</div>')
    desc = " → ".join(f"{k} {n}" for k, n, _ in steps)
    return f'<div class="lyst-flow" role="img" aria-label="{escape(label)}: {escape(desc)}">{"".join(boxes)}</div>'


def stats(items: Sequence[tuple[str, str, str | None]], *, compact: bool = False) -> str:
    """A strip of headline numbers: (label, value, note). Quieter than st.metric tiles, and one line tall.
    ``compact`` keeps every item on one row at body size, for use inside a card."""
    cells = "".join(f'<div class="lyst-stat"><div class="lyst-stat-value lyst-num">{escape(v)}</div>'
                    f'<div class="lyst-stat-label">{escape(lbl)}</div>'
                    + (f'<div class="lyst-stat-note">{escape(n)}</div>' if n else "") + "</div>"
                    for lbl, v, n in items)
    cls = "lyst-stats lyst-stats-compact" if compact else "lyst-stats"
    style = f' style="grid-template-columns: repeat({len(items)}, minmax(0, 1fr))"' if compact else ""
    return f'<div class="{cls}"{style}>{cells}</div>'


def delta(text: str, good: bool | None) -> str:
    """A change, coloured by whether it is good for this measure (not by its sign)."""
    tone = "neutral" if good is None else ("good" if good else "bad")
    arrow = "" if text.startswith(("0.0", "+0.0", "-0.0")) else ("↑ " if text.startswith("+") else
                                                                    "↓ " if text.startswith("-") else "")
    return f'<span class="lyst-delta lyst-num" style="color:var(--lyst-{tone})">{escape(arrow + text)}</span>'


def metric_tile(p: dict[str, Any], *, note: str = "vs prior month") -> str:
    """A KPI tile from a pulse entry: the measure, its period, the value large, the change coloured by whether it is
    good for this measure, and its recent history. An observation, so it says which period it observed."""
    return (f'<div class="lyst-card-title">{escape(str(p["title"]))}</div>'
            f'<div class="lyst-tile-value lyst-num">{escape(str(p["value_text"]))}</div>'
            + delta(f'{p.get("change_text", "")} {note}', p.get("good"))
            + spark(p.get("history") or [], label=f'{p["title"]} over the last {len(p.get("history") or [])} '
                    f'{p.get("grain", "period")}s, latest {p["value_text"]}', height=48))


STEP_MARK = {"done": ("✓", "good"), "failed": ("✕", "bad"), "skipped": ("–", "neutral"), "running": ("●", "info"),
             "queued": ("○", "neutral"), "pending": ("○", "neutral")}


def steps(items: Sequence[dict], *, label: str = "Steps", status_text: bool = True) -> str:
    """A task's steps as a vertical timeline, each with the engine's own status (never an estimate)."""
    rows = []
    for st_ in items:
        mark, tone = STEP_MARK.get(st_.get("status", ""), ("·", "neutral"))
        title = str(st_.get("capability", "step")).replace("_", " ").capitalize()
        who = str(st_.get("agent", "")).replace("_", " ")
        err = f'<div class="lyst-step-err">{escape(str(st_["error"]))}</div>' if st_.get("error") else ""
        rows.append(f'<li class="lyst-step"><span class="lyst-step-dot" style="color:var(--lyst-{tone});'
                    f'border-color:var(--lyst-{tone})">{mark}</span><div><div class="lyst-step-title">{escape(title)}</div>'
                    f'<div class="lyst-card-meta">{escape(who)}'
                    + (f' · {escape(str(st_.get("status", "")))}' if status_text else "") + f'</div>{err}</div></li>')
    done = sum(1 for x in items if x.get("status") == "done")
    return (f'<ol class="lyst-steps" aria-label="{escape(label)}: {done} of {len(items)} done">' + "".join(rows) + "</ol>")


def _img(svg: str, alt: str, *, max_w: int | None = None) -> str:
    """An SVG drawing as an image: never wider than drawn, so text keeps its size; narrower screens scale it down."""
    data = base64.b64encode(svg.encode()).decode()
    style = "width:100%;height:auto" + (f";max-width:{max_w}px" if max_w else "")
    return f'<img class="lyst-graph" src="data:image/svg+xml;base64,{data}" alt="{escape(alt)}" style="{style}">'


def _node(x: float, y: float, w: float, label: str, sub: str, c: dict, *, accent: bool = False) -> str:
    fill, stroke = (c["raised"], c["accent"] if accent else c["border"])
    lab = escape(label if len(label) <= 26 else label[:25] + "…")
    sb = escape(sub if len(sub) <= 28 else sub[:27] + "…")
    return (f'<rect x="{x - w / 2:.1f}" y="{y - 22:.1f}" width="{w:.1f}" height="44" rx="12" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{1.6 if accent else 1}"/>'
            f'<text x="{x:.1f}" y="{y - 3:.1f}" text-anchor="middle" font-family="-apple-system,Segoe UI,sans-serif" '
            f'font-size="13" font-weight="600" fill="{c["text"]}">{lab}</text>'
            f'<text x="{x:.1f}" y="{y + 13:.1f}" text-anchor="middle" font-family="-apple-system,Segoe UI,sans-serif" '
            f'font-size="10.5" fill="{c["text-muted"]}">{sb}</text>')


def layered_graph(layers: Sequence[tuple[str, Sequence[tuple[str, str, str]]]], edges: Sequence[tuple[str, str]],
                  *, label: str) -> str:
    """A left-to-right map of a few kinds of things and how they connect (sources → datasets → metrics → terms):
    readable at a glance, never a hairball. ``layers`` = [(heading, [(id, label, sub)])], ``edges`` = [(id, id)]."""
    c = tokens.colors(mode())
    lw, gap_y, top = 220, 60, 44
    rows = max((len(nodes) for _, nodes in layers), default=1)
    w, h = lw * len(layers), top + gap_y * rows + 10
    pos: dict[str, tuple[float, float]] = {}
    parts = []
    for li, (head, nodes) in enumerate(layers):
        cx = lw * li + lw / 2
        parts.append(f'<text x="{cx:.1f}" y="18" text-anchor="middle" font-family="-apple-system,Segoe UI,sans-serif" '
                     f'font-size="10.5" font-weight="700" letter-spacing=".08em" fill="{c["text-muted"]}">'
                     f'{escape(head.upper())}</text>')
        off = (rows - len(nodes)) * gap_y / 2
        for ni, (nid, _, _) in enumerate(nodes):
            pos[nid] = (cx, top + off + gap_y * ni + 22)
    lines = []
    for a, b in edges:
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            x1, x2 = x1 + 85, x2 - 85
            mx = (x1 + x2) / 2
            lines.append(f'<path d="M{x1:.1f},{y1:.1f} C{mx:.1f},{y1:.1f} {mx:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" '
                         f'fill="none" stroke="{c["accent"]}" stroke-opacity=".45" stroke-width="1.5"/>')
    for li, (_, nodes) in enumerate(layers):
        for nid, lab, sub in nodes:
            x, y = pos[nid]
            parts.append(_node(x, y, 170, lab, sub, c, accent=li == len(layers) // 2))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
           + "".join(lines) + "".join(parts) + "</svg>")
    names = {nid: lab for _, nodes in layers for nid, lab, _ in nodes}
    desc = "; ".join(f"{names.get(a, a)} to {names.get(b, b)}" for a, b in edges if a in names and b in names)
    return _img(svg, f"{label}. Connections: {desc or 'none'}.", max_w=int(w * 1.15))


def ring_graph(nodes: Sequence[tuple[str, str, str]], edges: Sequence[tuple[str, str, str]], *, label: str) -> str:
    """A few things arranged on a ring with labelled links between them (datasets and the columns they share)."""
    import math  # noqa: PLC0415

    c = tokens.colors(mode())
    n = max(len(nodes), 1)
    w, h = 760, 380 if n > 2 else 220
    cx, cy, rx, ry = w / 2, h / 2, w / 2 - 110, h / 2 - 50
    pos = {nid: (cx + rx * math.cos(2 * math.pi * i / n - math.pi / 2) if n > 1 else cx,
                 cy + ry * math.sin(2 * math.pi * i / n - math.pi / 2) if n > 1 else cy)
           for i, (nid, _, _) in enumerate(nodes)}
    parts = []
    for a, b, lab in edges:
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{c["accent"]}" '
                         f'stroke-opacity=".45" stroke-width="1.6"/>')
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            t = escape(lab if len(lab) <= 30 else lab[:29] + "…")
            parts.append(f'<rect x="{mx - 4 * len(t) - 8:.1f}" y="{my - 10:.1f}" width="{8 * len(t) + 16:.1f}" height="20" '
                         f'rx="10" fill="{c["bg"]}" stroke="{c["border"]}"/>'
                         f'<text x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle" font-family="ui-monospace,Menlo,monospace" '
                         f'font-size="10.5" fill="{c["text-muted"]}">{t}</text>')
    for nid, lab, sub in nodes:
        x, y = pos[nid]
        parts.append(_node(x, y, 180, lab, sub, c, accent=True))
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">' + "".join(parts) + "</svg>"
    names = {nid: lab for nid, lab, _ in nodes}
    desc = "; ".join(f"{names.get(a, a)} and {names.get(b, b)} share {lab}" for a, b, lab in edges)
    return _img(svg, f"{label}. {desc or 'No shared columns found'}.", max_w=int(w * 1.15))


CSS = """
.lyst-spark {display: block;}
.lyst-viz-empty {color: var(--lyst-text-muted); font-size: var(--lyst-size-sm);}
.lyst-stack {display: flex; gap: 2px; border-radius: var(--lyst-radius-pill); overflow: hidden;}
.lyst-stack span {display: block; height: 100%;}
.lyst-legends {display: flex; flex-wrap: wrap; gap: .2rem .8rem; margin-top: .35rem; font-size: var(--lyst-size-xs);
               color: var(--lyst-text-muted);}
.lyst-legend i {display: inline-block; width: .55rem; height: .55rem; border-radius: 2px; margin-right: .3rem;
                vertical-align: baseline;}
.lyst-meter {height: 6px; border-radius: var(--lyst-radius-pill); background: var(--lyst-grid); overflow: hidden;}
.lyst-meter span {display: block; height: 100%; border-radius: var(--lyst-radius-pill);}
.lyst-meter-text {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted); margin-top: .2rem;}
.lyst-bars {display: grid; gap: .3rem;}
.lyst-bar {display: grid; grid-template-columns: minmax(5rem, 32%) 1fr 3.2rem; align-items: center; gap: .5rem;
           font-size: var(--lyst-size-sm);}
.lyst-bar-label {overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
.lyst-bar-track {height: 8px; background: var(--lyst-grid); border-radius: var(--lyst-radius-pill); overflow: hidden;}
.lyst-bar-track span {display: block; height: 100%; border-radius: var(--lyst-radius-pill);}
.lyst-bar-value {text-align: right; color: var(--lyst-text-muted);}
.lyst-flow {display: flex; flex-wrap: wrap; align-items: stretch; gap: .5rem;}
.lyst-flow-node {border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-md); padding: .5rem .7rem;
                 min-width: 9rem; flex: 1 1 9rem; background: var(--lyst-surface);}
.lyst-flow-kind {font-size: var(--lyst-size-xs); text-transform: uppercase; letter-spacing: var(--lyst-tracking-label);
                 color: var(--lyst-text-muted); font-weight: 600;}
.lyst-flow-name {font-weight: var(--lyst-weight-medium); margin-top: .1rem;}
.lyst-flow-detail {font-size: var(--lyst-size-sm); color: var(--lyst-text-muted);}
.lyst-flow-arrow {align-self: center; color: var(--lyst-text-muted);}
.lyst-stats {display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .75rem 1.5rem;
             padding: .25rem 0 .5rem 0;}
.lyst-stats-compact {gap: .5rem 1rem; padding: .5rem 0;}
.lyst-stats-compact .lyst-stat-value {font-size: var(--lyst-size-md);}
.lyst-stat-value {font-size: var(--lyst-size-lg); font-weight: var(--lyst-weight-strong); line-height: 1.2;}
.lyst-stat-label {font-size: var(--lyst-size-sm); color: var(--lyst-text-muted);}
.lyst-stat-note {font-size: var(--lyst-size-xs); color: var(--lyst-text-muted);}
.lyst-tile-value {font-size: 2rem; font-weight: var(--lyst-weight-strong); letter-spacing: -0.02em; line-height: 1.1;
                  margin: .15rem 0 .1rem 0;}
.lyst-delta {font-size: var(--lyst-size-sm); font-weight: var(--lyst-weight-medium);}
/* the evidence rail beside an answer: quiet sections separated by space, not boxes */
.lyst-rail {display: grid; gap: 1rem; padding: .2rem 0 .2rem 1rem; border-left: 1px solid var(--lyst-border);}
.lyst-rail-section {display: grid; gap: .35rem;}
.lyst-rail-item {padding: .1rem 0;}
@media (max-width: 640px) {.lyst-rail {border-left: none; padding-left: 0; border-top: 1px solid var(--lyst-border);
                           padding-top: .8rem;}}
/* a task's steps: a quiet vertical timeline */
.lyst-steps {list-style: none; margin: 0; padding: 0; position: relative;}
.lyst-steps::before {content: ""; position: absolute; left: .7rem; top: .6rem; bottom: .6rem; width: 1px;
                     background: var(--lyst-border);}
.lyst-step {display: grid; grid-template-columns: 1.4rem 1fr; gap: .6rem; padding: .3rem 0; position: relative;
             margin: 0 !important;}
.lyst-step-dot {width: 1.4rem; height: 1.4rem; border-radius: 50%; border: 1.5px solid; background: var(--lyst-raised);
                display: grid; place-items: center; font-size: .72rem; font-weight: 700; z-index: 1;}
.lyst-step-title {font-weight: var(--lyst-weight-medium); font-size: var(--lyst-size-sm);}
.lyst-step-err {color: var(--lyst-bad); font-size: var(--lyst-size-sm);}
/* Work board: columns of compact task cards */
.lyst-col-head {display: flex; justify-content: space-between; align-items: baseline; margin-bottom: .3rem;}
.lyst-formula {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: var(--lyst-size-xs);
               background: var(--lyst-surface); border-radius: var(--lyst-radius-sm); padding: .3rem .5rem;
               display: inline-block; color: var(--lyst-text);}
.lyst-tags {display: flex; flex-wrap: wrap; gap: .3rem; margin: .45rem 0 .2rem 0;}
.lyst-tag {font-size: var(--lyst-size-xs); border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-pill);
           padding: .05rem .5rem; color: var(--lyst-text-muted);}
img.lyst-graph {display: block; margin: .4rem auto 0 auto;}
/* cards: a title row, a headline, a visual, a quiet footer */
.lyst-card-kicker {font-size: var(--lyst-size-xs); text-transform: uppercase; letter-spacing: var(--lyst-tracking-label);
                   color: var(--lyst-text-muted); font-weight: 600;}
.lyst-card-title {font-weight: var(--lyst-weight-strong); font-size: 1.02rem; line-height: 1.3;}
.lyst-card-value {font-size: 1.45rem; font-weight: var(--lyst-weight-strong); line-height: 1.15; margin-top: .15rem;}
.lyst-card-meta {font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); line-height: 1.4;}
"""


def pulse(api: Any, ws: str) -> dict[str, dict]:
    """Each approved metric's latest value, change and twelve-period history (GET pulse), by metric name. Cached per
    workspace for five minutes in this session, the same freshness Home uses; never shared across workspaces."""
    import time  # noqa: PLC0415

    from lyst.ui.client import APIError  # noqa: PLC0415

    hit = st.session_state.get("_viz_pulse")
    if hit and hit[0] == ws and time.time() - hit[1] < 300:
        return hit[2]
    try:
        data = {p["metric"]: p for p in api.get(f"/v1/workspaces/{ws}/pulse")}
    except APIError:
        data = {}
    st.session_state["_viz_pulse"] = (ws, time.time(), data)
    return data
