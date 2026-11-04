# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What if…? Move the drivers of a metric and see the effect, with the observed base it starts from, how sensitive
the result is, and a saved, reproducible copy. Reads the session on every call."""

from __future__ import annotations

import streamlit as st

from lyst.ui import kit
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md


def _ctx():
    return client(), workspace(), st.session_state.get("role") in ("editor", "admin", "owner")


def fmt(v: float | None, kind: str) -> str:
    if v is None:
        return "n/a"
    if kind == "percent":
        return f"{v * 100:.1f}%"
    if kind == "currency":
        a = abs(v)
        return ("-" if v < 0 else "") + (f"${a / 1e6:.2f}M" if a >= 1e6 else f"${a / 1e3:.0f}K" if a >= 1e3 else f"${a:,.0f}")
    return f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}"


def delta(o: dict) -> str:
    if o.get("format") == "percent":
        return f"{o['delta'] * 100:+.1f} pts"
    return f"{o.get('delta_pct') or 0:+.1f}%"


def models_for(metric: str) -> list[str]:
    api, ws, _ = _ctx()
    try:
        return api.get(f"/v1/workspaces/{ws}/metrics/{metric}/scenario-models")
    except APIError:
        return []


@st.dialog("What if…?", width="large")
def explore(name: str, overrides: dict | None = None) -> None:
    api, ws, editor = _ctx()
    try:
        m = next(x for x in api.get(f"/v1/workspaces/{ws}/scenario-models") if x["name"] == name)
    except (APIError, StopIteration):
        st.error("This scenario model is not available.")
        return
    st.markdown(md(f"**{m['title']}**"))
    st.caption("Move a driver; Lyst recomputes from the latest observed figures. The results are hypothetical.")
    ov: dict = {}
    for inp in m["inputs"]:
        k = f"sc_{name}_{inp['name']}"
        if inp.get("observed") and inp.get("range_pct"):
            lo, hi = inp["range_pct"]
            start = ((overrides or {}).get(inp["name"]) or {}).get("change_pct", 0.0)
            v = st.slider(f"{inp['label']}: change", float(min(lo, -20)), float(max(hi, 20)), float(start), 0.5,
                          format="%+.1f%%", key=k, help=f"Usual range {lo:+g}% to {hi:+g}%.")
            if v:
                ov[inp["name"]] = {"change_pct": v}
        elif inp.get("low") is not None and inp.get("high") is not None:
            start = ((overrides or {}).get(inp["name"]) or {}).get("value", inp["value"])
            v = st.slider(inp["label"], float(inp["low"]), float(inp["high"]), float(start), 0.01, key=k,
                          format="%.2f", help="An assumption, not an observed figure.")
            if abs(v - float(inp["value"])) > 1e-9:
                ov[inp["name"]] = {"value": v}
    prev = st.session_state.get(f"sc_prev_{name}")
    body = {"overrides": ov} | ({"previous": prev} if prev else {})
    r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/scenario-models/{name}/evaluate", body))
    if not r:
        return
    st.session_state[f"sc_prev_{name}"] = r
    kit.label("Result")
    for i, o in enumerate(r.get("outputs") or []):
        tone = None if abs(o.get("delta") or 0) < 1e-12 else (("Better", "ok") if o.get("favourable") else ("Worse", "bad"))
        kit.row(o["label"], f"{fmt(o['base'], o['format'])} → {fmt(o['scenario'], o['format'])} ({delta(o)})",
                status=tone, key=f"sco{i}", divider=i < len(r["outputs"]) - 1)
    for v in r.get("validation") or []:
        if v.get("status") != "pass":
            (st.error if v["status"] == "fail" else st.warning)(md(v.get("detail") or v["check"]))
    with st.expander("Where the starting figures come from", icon=":material/database:"):
        for k2, o in (r.get("observed") or {}).items():
            st.caption(md(f"{k2.replace('_', ' ')}: {o.get('value'):,.4g} · {(o.get('period') or {}).get('label', '')} "
                          f"from {o.get('dataset')} v{o.get('dataset_version')} · data through {o.get('data_through')}"))
        for k2, a in (r.get("assumptions") or {}).items():
            st.caption(md(f"{k2.replace('_', ' ')}: assumed {a.get('value')} (between {a.get('low')} and {a.get('high')})"))
    c = st.columns(2)
    if c[0].button("How sensitive is it?", icon=":material/tune:", key=f"sc_an_btn_{name}", width="stretch"):
        st.session_state[f"sc_an_{name}"] = kit.guarded(lambda: api.post(
            f"/v1/workspaces/{ws}/scenario-models/{name}/analyze", {"overrides": ov, "draws": 2000, "seed": 7}))
    an = st.session_state.get(f"sc_an_{name}")
    if an:
        out = (an.get("cases") or {}).get("output") or (m.get("outputs") or [""])[0]
        f = next((o["format"] for o in r["outputs"] if o["name"] == out), "number")
        kit.label("What moves it most")
        for i, srow in enumerate((an.get("sensitivity") or [])[:4]):
            kit.row(srow["label"], f"across its range the result goes from {fmt(srow['output_at_low'], f)} to "
                    f"{fmt(srow['output_at_high'], f)}", key=f"scs{i}", divider=i < 3)
        cases = an.get("cases") or {}
        if cases:
            st.caption(md(f"Worst case {fmt(cases['worst']['value'], f)}, base {fmt(cases['base']['value'], f)}, "
                          f"best case {fmt(cases['best']['value'], f)}."))
        sim = ((an.get("simulation") or {}).get("outputs") or {}).get(out)
        if sim:
            st.caption(md(f"Across {an['simulation']['draws']:,} random combinations of the uncertain drivers, 90% of "
                          f"results fall between {fmt(sim['p5'], f)} and {fmt(sim['p95'], f)} (middle {fmt(sim['p50'], f)})."))
    if editor and ov:
        with c[1].popover("Save this scenario", icon=":material/save:", width="stretch"):
            sname = st.text_input("Name", placeholder="Price up 3%, opex up 6%", key=f"sc_name_{name}")
            note = st.text_input("Note (optional)", key=f"sc_note_{name}")
            if st.button("Save", type="primary", key=f"sc_save_{name}") and sname:
                kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/scenario-models/{name}/scenarios",
                                             {"name": sname, "overrides": ov, "note": note}), "Saved")


def saved_list() -> None:
    """Data › Saved analyses: the scenario models and saved scenarios, each reproducible on today's data."""
    api, ws, _ = _ctx()
    try:
        models = api.get(f"/v1/workspaces/{ws}/scenario-models")
    except APIError:
        return
    if not models:
        return
    kit.label(f"Scenarios · {len(models)} model{'s' * (len(models) != 1)}")
    for m in models:
        if kit.row(m["title"], "Drivers: " + ", ".join(x["label"] for x in m["inputs"]), actions=[("Explore", "go")],
                   key=f"scm{m['name']}", divider=True):
            st.session_state.pop(f"sc_prev_{m['name']}", None)
            explore(m["name"])
        try:
            saved = api.get(f"/v1/workspaces/{ws}/scenario-models/{m['name']}/scenarios")
        except APIError:
            saved = []
        for j, s in enumerate(saved):
            hit = kit.row(f"↳ {s['name']}", " · ".join(x for x in (s.get("note"), f"v{s.get('version')}",
                                                                   kit.when(s.get("created_at"))) if x),
                          actions=[("Open", "open"), ("Re-run", "rep")], key=f"scs{s['id']}", divider=j < len(saved) - 1)
            if hit == "open":
                explore(m["name"], s.get("overrides"))
            elif hit == "rep":
                out = kit.guarded(lambda x=s: api.post(f"/v1/workspaces/{ws}/scenarios/{x['id']}/reproduce"))
                if out:
                    st.toast("Same result as when it was saved" if not out.get("differences") else
                             "Different now: " + "; ".join(map(str, out["differences"]))[:200], icon=":material/replay:")
