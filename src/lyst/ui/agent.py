# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The agentic layer every page shares: what Lyst is doing right now, said once and the same way everywhere.

* :func:`state`: the workspace's live state (running work, what waits on the person, schedules and watched metrics),
  read at most every ``TTL`` seconds and dropped at once when the person acts (``kit.guarded``).
* :func:`pulse`: one calm status line with a live dot ("Working on 2 requests · watching 3 metrics · next run in
  3 h"), and the way into Brain › Work.
* :func:`answers`: the latest verified answers as answer-first cards: the question, the answer itself, a preview
  drawn from its own numbers; the evidence and what to do fold open in place, and Open shows the full artifact.
* :func:`hero`: Brain's opening band: what Lyst knows, what it is doing, and what it runs on its own, over a drawing
  made from those same counts.

Every number here is a count of engine records; nothing is estimated or animated into existence.
"""

from __future__ import annotations

import base64
import math
import time
from html import escape
from typing import Any

import streamlit as st

from lyst.ui import kit, tokens, visuals
from lyst.ui.client import APIError, client
from lyst.ui.components import bullets, md, trust_line

TTL = 20  # seconds, as the sidebar's waiting count
ACTIVE = {"queued", "running", "paused", "awaiting_approval"}

CSS = """
/* the live status line: a dot that breathes while Lyst works, then one sentence */
.lyst-pulse {display: flex; align-items: center; gap: .6rem; font-size: var(--lyst-size-sm); color: var(--lyst-text-muted);
  padding: .1rem 0; line-height: 1.4;}
.lyst-pulse b {color: var(--lyst-text); font-weight: var(--lyst-weight-medium);}
.lyst-pulse-dot, .lyst-live-dot {position: relative; flex: none; width: .55rem; height: .55rem; border-radius: 50%;
  background: var(--lyst-good); display: inline-block;}
.lyst-live-dot {margin-right: .4rem; vertical-align: .05rem; background: var(--lyst-info);}
.lyst-pulse-dot.run {background: var(--lyst-info);}
.lyst-pulse-dot.wait {background: var(--lyst-warn);}
.lyst-pulse-dot.run::after, .lyst-live-dot::after {content: ""; position: absolute; inset: -4px; border-radius: 50%;
  border: 2px solid var(--lyst-info); opacity: 0; animation: lyst-ping 1.8s var(--lyst-motion-ease) infinite;}
@keyframes lyst-ping {0% {transform: scale(.6); opacity: .7;} 100% {transform: scale(1.6); opacity: 0;}}
/* an answer card: the answer first, its preview beside it, details folded underneath */
.lyst-answer-q {font-size: var(--lyst-size-sm); color: var(--lyst-text-muted); margin: .1rem 0 .4rem 0;
  display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;}
.lyst-answer-badge {font-size: var(--lyst-size-xs); font-weight: 600; border-radius: var(--lyst-radius-pill);
  padding: .1rem .55rem; border: 1px solid; white-space: nowrap;}
.lyst-answer-badge.ok {color: var(--lyst-good); border-color: color-mix(in srgb, var(--lyst-good) 40%, transparent);
  background: color-mix(in srgb, var(--lyst-good) 8%, transparent);}
.lyst-answer-badge.warn {color: var(--lyst-warn); border-color: color-mix(in srgb, var(--lyst-warn) 45%, transparent);
  background: color-mix(in srgb, var(--lyst-warn) 8%, transparent);}
div.lyst-answer-text p {font-size: var(--lyst-size-lg); font-weight: var(--lyst-weight-medium); line-height: 1.4;
  margin: 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;}
img.lyst-answer-thumb {width: 100%; max-width: 220px; aspect-ratio: 240 / 132; border-radius: var(--lyst-radius-md);
  display: block; margin-left: auto;}
div[class*="st-key-lyst_card_ans_"] [data-testid="stExpander"] details {box-shadow: none; background: transparent;
  border-color: var(--lyst-border) !important;}
@media (max-width: 640px) {img.lyst-answer-thumb {display: none;}}
/* Brain's opening band: a raised surface with an accent glow; the words on the left, the knowledge drawing on the right */
.lyst-hero {position: relative; overflow: hidden; border-radius: var(--lyst-radius-xl); border: 1px solid var(--lyst-edge);
  background: radial-gradient(90% 140% at 100% 0%, color-mix(in srgb, var(--lyst-accent) 20%, transparent), transparent 62%),
              radial-gradient(60% 90% at 0% 100%, color-mix(in srgb, var(--lyst-info) 9%, transparent), transparent 70%),
              var(--lyst-raised);
  box-shadow: var(--lyst-shadow-2); padding: 1.5rem 1.6rem 1.2rem 1.6rem; margin-bottom: .4rem;
  display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 1rem; align-items: center;}
.lyst-hero-body {position: relative; z-index: 1;}
.lyst-hero img.lyst-constellation {width: 100%; max-width: 560px; justify-self: end;
  animation: lyst-drift 24s ease-in-out infinite alternate;}
@keyframes lyst-drift {from {transform: rotate(-1.5deg) scale(1);} to {transform: rotate(1.5deg) scale(1.04);}}
.lyst-hero-eyebrow {display: inline-flex; align-items: center; gap: .5rem; font-size: var(--lyst-size-xs);
  text-transform: uppercase; letter-spacing: var(--lyst-tracking-label); font-weight: 600; color: var(--lyst-text-muted);
  border: 1px solid var(--lyst-border); border-radius: var(--lyst-radius-pill); padding: .2rem .7rem .2rem .55rem;
  background: var(--lyst-material); margin-bottom: .7rem;}
.lyst-hero h1.lyst-title {font-size: 2.4rem;}
dl.lyst-hero-groups {margin: 1.1rem 0 0 0; padding: 0;}
.lyst-hero-group {display: grid; grid-template-columns: 6.5rem minmax(0, 1fr); align-items: baseline; gap: .6rem;
  padding: .5rem 0; border-top: 1px solid var(--lyst-border);}
.lyst-hero-group dt {margin: 0; font-size: var(--lyst-size-xs); text-transform: uppercase;
  letter-spacing: var(--lyst-tracking-label); color: var(--lyst-text-muted); font-weight: 600;}
.lyst-hero-group dd {margin: 0; display: flex; flex-wrap: wrap; gap: .2rem 1rem;}
.lyst-hero-num {display: inline-flex; align-items: baseline; gap: .3rem; font-size: var(--lyst-size-sm);
  color: var(--lyst-text-muted);}
.lyst-hero-num b {font-size: 1.3rem; color: var(--lyst-text); font-weight: var(--lyst-weight-strong);
  font-variant-numeric: tabular-nums;}
.lyst-hero-num.warn b {color: var(--lyst-warn);}
@media (max-width: 760px) {.lyst-hero {grid-template-columns: 1fr;} .lyst-hero img.lyst-constellation {display: none;}}
/* Brain's sections: icon pills, the open one raised with its icon in the accent */
div.st-key-brain_nav [role="radiogroup"] {background: transparent; padding: 0; gap: .35rem; flex-wrap: wrap;}
div.st-key-brain_nav button[data-variant="segmented_control"] {border-radius: var(--lyst-radius-pill) !important;
  padding: 0 .95rem; min-height: 2.25rem; color: var(--lyst-text-muted);}
div.st-key-brain_nav button[data-variant="segmented_control"]:hover {background: var(--lyst-raised); color: var(--lyst-text);}
div.st-key-brain_nav button[aria-checked="true"] {box-shadow: var(--lyst-shadow-2); color: var(--lyst-text) !important;
  font-weight: var(--lyst-weight-medium);}
div.st-key-brain_nav button[aria-checked="true"] [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
"""


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ------------------------------------------------------------------------------------------------- state

def state(ws: str) -> dict[str, Any]:
    """Tasks, schedules and watched metrics for ``ws``, read at most every TTL seconds (``kit.guarded`` drops it)."""
    hit = st.session_state.get("_agent")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < TTL:
        return hit[2]
    api = client()
    out: dict[str, Any] = {"tasks": [], "schedules": [], "monitors": []}
    for k, path in (("tasks", "tasks"), ("schedules", "schedules"), ("monitors", "monitors")):
        try:
            out[k] = api.get(f"/v1/workspaces/{ws}/{path}") or []
        except APIError:
            pass
    st.session_state["_agent"] = (ws, time.monotonic(), out)
    return out


def waiting(s: dict[str, Any]) -> int:
    """Everything that waits on this person: the sidebar's count (approvals, findings, assignments, reviews, prepared
    work) plus answers that need their input or did not pass verification."""
    return int(st.session_state.get("waiting") or 0) + sum(
        1 for t in s["tasks"][:30] if t.get("outcome") in ("needs_input", "unverified"))


def summary(s: dict[str, Any], need: int) -> tuple[str, str, list[str]]:
    """(tone, headline, details) for the status line: 'run', 'wait' or 'ok', then what Lyst is doing in words."""
    tasks = s["tasks"]
    running = sum(t["status"] in ACTIVE for t in tasks)
    watching = sum(1 for m in s["monitors"] if m.get("enabled"))
    repeating = sum(1 for x in s["schedules"] if x.get("enabled"))
    nxt = min((x["next_run_at"] for x in s["schedules"] if x.get("enabled") and x.get("next_run_at")), default=None)
    done = [t for t in tasks if t["status"] == "done"]
    if running:
        tone, head = "run", f"Working on {_plural(running, 'request')}"
    elif need:
        tone, head = "wait", f"{_plural(need, 'thing')} need{'s' * (need == 1)} you"
    else:
        tone, head = "ok", "Up to date"
    parts = []
    if running and need:
        parts.append(f"{need} need{'s' * (need == 1)} you")
    if watching:
        parts.append(f"watching {_plural(watching, 'metric')}")
    if repeating:
        parts.append(f"{_plural(repeating, 'question')} on a schedule")
    if nxt:
        parts.append(f"next run {kit.when(nxt)}")
    if done:
        parts.append(f"last answer {kit.when(done[0].get('updated_at') or done[0].get('created_at'))}")
    if not (watching or repeating):
        parts.append("nothing runs on its own yet")
    return tone, head, parts


def pulse(ws: str, *, key: str, link: bool = True) -> None:
    """One line on what Lyst is doing now, with a live dot; the button opens Brain › Work."""
    s = state(ws)
    n = waiting(s)
    tone, head, parts = summary(s, n)
    html = (f'<div class="lyst-pulse" role="status"><span class="lyst-pulse-dot {tone}" aria-hidden="true"></span>'
            f'<span><b>{escape(head)}</b>' + "".join(f" · {escape(p)}" for p in parts) + "</span></div>")
    if not link:
        st.html(html)
        return
    c = st.columns([6, 1.5], vertical_alignment="center")
    c[0].html(html)
    if c[1].button("Lyst's work", key=f"pulse_{key}", type="tertiary", icon=":material/arrow_forward:",
                   width="stretch", help="Everything Lyst is doing, has done and runs on its own (Brain › Work)"):
        from lyst.ui import work  # noqa: PLC0415

        work.go("needs you" if n and tone == "wait" else None)


# ------------------------------------------------------------------------------------------------ answers

@st.cache_data(ttl=3600, show_spinner=False)
def _thumb(mode: str, ys: tuple, bars: bool) -> str:
    from lyst.ui import artifacts  # noqa: PLC0415

    return artifacts._svg(artifacts._line(list(ys), mode, bars=bars), mode) if len(ys) >= 2 else \
        artifacts._svg(artifacts._glyph("report", mode), mode)


def _series(r: dict) -> tuple[tuple, bool]:
    """The numbers an answer's preview is drawn from: its first chart, else its first numeric table column."""
    for ch in r.get("charts") or []:
        ys = (ch.get("history_y", []) + ch.get("y", [])) if ch.get("type") == "forecast" else ch.get("y", [])
        ys = tuple(float(v) for v in ys if isinstance(v, int | float))
        if len(ys) >= 2:
            return ys, ch.get("type") == "bar"
    for t in r.get("tables") or []:
        nums = tuple(float(row[-1]) for row in t.get("rows", []) if row and isinstance(row[-1], int | float))
        if len(nums) >= 2:
            return nums[:12], True
    return (), False


def answer_card(t: dict, r: dict, *, key: str) -> bool:
    """One answer, answer first. Returns True when Open (the full artifact) was pressed."""
    level = (r.get("confidence") or {}).get("level")
    question = ((t.get("request") or {}).get("question") or t.get("title") or "").strip()
    with kit.card(f"ans_{key}"):
        left, right = st.columns([3.3, 1], vertical_alignment="top")
        with left:
            badge, tone = (("Needs your input", "warn") if r.get("status") == "needs_input" else
                           ("Verified", "ok") if level == "verified" else ((level or "").capitalize(), "warn"))
            st.html('<div class="lyst-answer-q">' + (f'<span class="lyst-answer-badge {tone}">{escape(badge)}</span>'
                    if badge else "") + f'{escape(question)} · {escape(kit.when(t.get("updated_at") or t.get("created_at")))}'
                    "</div>")
            st.markdown(f'<div class="lyst-answer-text">\n\n{kit._md_text(r.get("brief") or r.get("answer") or "")}'
                        "\n\n</div>", unsafe_allow_html=True)
        ys, bars = _series(r)
        right.html(f'<img class="lyst-answer-thumb" src="{_thumb(visuals.mode(), ys, bars)}" '
                   f'alt="Preview of the answer to {escape(question)}">')
        foot = st.columns([1.1, 5], vertical_alignment="top")
        opened = foot[0].button("Open", key=f"ans_open_{key}", type="tertiary", icon=":material/open_in_full:",
                                help="The full answer, with its evidence, charts and what you can do next")
        with foot[1].expander("Why, and what to do", icon=":material/unfold_more:"):
            if tl := trust_line(r):
                st.caption(md(tl))
            if r.get("evidence"):
                st.markdown(bullets(r["evidence"][:4]))
            if r.get("actions"):
                st.markdown("**What to do**")
                st.markdown(bullets(r["actions"][:3]))
            if not (r.get("evidence") or r.get("actions")):
                st.caption("Open the answer for its evidence and method.")
    return opened


def answers(tasks: list[dict], *, n: int = 3, title: str = "Latest answers") -> list[dict]:
    """The newest finished answers as cards. Returns the finished tasks not shown, for a quieter list below."""
    from lyst.ui import artifacts  # noqa: PLC0415

    done = [t for t in tasks if t["status"] == "done" and t.get("kind", "analysis") in ("analysis", "scheduled", "workflow")
            and t.get("outcome") not in ("needs_input", "unverified")]
    shown = 0
    rest = []
    for t in done:
        if shown >= n:
            rest.append(t)
            continue
        r = artifacts.result(t["id"])
        if not r or not (r.get("answer") or r.get("brief")):
            rest.append(t)
            continue
        if not shown:
            kit.label(title)
        shown += 1
        if answer_card(t, r, key=t["id"]):
            st.session_state.task_id = t["id"]
            st.query_params["task"] = t["id"]
            st.rerun()
    return rest


# --------------------------------------------------------------------------------------------- Brain hero

def constellation(groups: list[tuple[str, int]], mode: str) -> str:
    """A drawing of what Lyst knows: one point per record (up to 24 per kind), each kind its own colour and arc,
    joined to its neighbours. Decorative; the same counts are in the text beside it."""
    c = tokens.colors(mode)
    pal = [tokens.series(mode)[i] for i in (0, 1, 3, 5, 2)]  # the alarm red is never decoration
    w, h = 560, 300
    cx, cy = w * .55, h * .5
    pts: list[tuple[float, float, str, float]] = []
    total = max(sum(min(n, 24) for _, n in groups), 1)
    start = 0.0
    for gi, (_, n) in enumerate(groups):
        k = min(n, 24)
        if not k:
            continue
        span = 2 * math.pi * k / total
        col = pal[gi % len(pal)]
        for i in range(k):
            a = start + span * (i + .5) / k
            rad = 70 + 62 * ((i * 0.618 + gi * 0.37) % 1)  # golden-ratio spread: stable, never random
            pts.append((cx + rad * math.cos(a) * 1.55, cy + rad * math.sin(a) * .95, col, 3.2 + (i % 3)))
        start += span
    edges = set()
    for i, (x, y, _, _) in enumerate(pts):  # each point to its two nearest: a web, not a hairball
        for _, j in sorted(((x - x2) ** 2 + (y - y2) ** 2, j) for j, (x2, y2, _, _) in enumerate(pts) if j != i)[:2]:
            edges.add((min(i, j), max(i, j)))
    lines = [f'<line x1="{pts[i][0]:.1f}" y1="{pts[i][1]:.1f}" x2="{pts[j][0]:.1f}" y2="{pts[j][1]:.1f}" '
             f'stroke="{pts[i][2]}" stroke-opacity=".28" stroke-width="1"/>' for i, j in sorted(edges)]
    spokes = "".join(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="{c["border"]}" '
                     f'stroke-opacity=".55" stroke-width=".8"/>' for x, y, _, _ in pts[::3])
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r + 3:.1f}" fill="{col}" fill-opacity=".14"/>'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{col}"/>' for x, y, col, r in pts)
    core = (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="26" fill="{c["accent"]}" fill-opacity=".12"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="14" fill="{c["accent"]}" fill-opacity=".9"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{c["raised"]}"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">' + spokes + "".join(lines) + dots + core
           + "</svg>")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


@st.cache_data(ttl=TTL, show_spinner=False)
def _knowledge(ws: str, token: str) -> dict[str, int]:
    api = client()
    out = {}
    for k, path in (("metrics", "metrics"), ("datasets", "datasets"), ("documents", "documents"),
                    ("memory", "memory"), ("terms", "glossary")):
        try:
            out[k] = len(api.get(f"/v1/workspaces/{ws}/{path}") or [])
        except APIError:
            out[k] = 0
    return out


def knowledge(ws: str) -> dict[str, int]:
    """How much Lyst knows here, by kind. Cached per workspace and person for TTL seconds."""
    return _knowledge(ws, str(st.session_state.get("me_id") or st.session_state.get("token") or ""))


def _num(n: int, word: str, tone: str = "") -> str:
    return f'<span class="lyst-hero-num {tone}"><b>{n:,}</b>{escape(word)}</span>'


def hero(ws: str) -> None:
    """Brain's opening band: the title, a live line, and what Lyst knows, does and runs on its own."""
    k = knowledge(ws)
    s = state(ws)
    need = waiting(s)
    tone, head, _ = summary(s, need)
    tasks = s["tasks"]
    running = sum(t["status"] in ACTIVE for t in tasks)
    week = time.time() - 7 * 86400

    def recent(t: dict) -> bool:
        from datetime import datetime  # noqa: PLC0415

        try:
            return datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00")).timestamp() >= week
        except (KeyError, ValueError):
            return False

    finished = sum(t["status"] == "done" and recent(t) for t in tasks)
    watching = sum(1 for m in s["monitors"] if m.get("enabled"))
    repeating = sum(1 for x in s["schedules"] if x.get("enabled"))
    groups = [("approved metrics", k["metrics"]), ("datasets", k["datasets"]), ("documents", k["documents"]),
              ("memories", k["memory"]), ("terms", k["terms"])]
    mode = visuals.mode()
    alt = "What Lyst knows: " + ", ".join(f"{n} {w}" for w, n in groups)
    knows = "".join(_num(n, w) for w, n in groups if n) or _num(0, "records yet")
    doing = (_num(running, "running") + _num(need, "need you", "warn" if need else "")
             + _num(finished, "finished this week"))
    own = _num(watching, "metrics watched") + _num(repeating, "scheduled questions")
    st.html(f'<section class="lyst-hero" aria-label="Brain overview"><div class="lyst-hero-body">'
            f'<div class="lyst-hero-eyebrow"><span class="lyst-pulse-dot {tone}" aria-hidden="true"></span>'
            f'{escape(head)}</div><h1 class="lyst-title">Brain</h1>'
            f'<p class="lyst-sub">What Lyst knows about this business, what it is doing with it, and what it runs on '
            f'its own.</p>'
            f'<dl class="lyst-hero-groups"><div class="lyst-hero-group"><dt>Knows</dt><dd>{knows}</dd></div>'
            f'<div class="lyst-hero-group"><dt>Doing</dt><dd>{doing}</dd></div>'
            f'<div class="lyst-hero-group"><dt>On its own</dt><dd>{own}</dd></div></dl></div>'
            f'<img class="lyst-constellation" src="{constellation(groups, mode)}" alt="{escape(alt)}"></section>')
