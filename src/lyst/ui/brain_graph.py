# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain › Graph: everything Lyst knows as one interactive map, the graph runs that plan, build, check and fix in
loops, and the harness that governs them (fixed rules, evidence ladder, failure policy, learned rules).

The canvas is a small custom component (``brain_graph_component``) with no library and no network: a force layout for
the knowledge map, fixed positions for a run's flow, a keyboard list as the accessible alternative, and no physics
animation under prefers-reduced-motion. Clicking a node sends its id back here; the side panel shows it with its
backlinks and opens the existing view for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from lyst.ui import kit, tokens, visuals
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

_canvas = components.declare_component("lyst_brain_graph", path=str(Path(__file__).parent / "brain_graph_component"))

KIND_LABELS = {
    "metric": "Metric", "dataset": "Dataset", "term": "Business term", "document": "Document", "memory": "Memory",
    "analysis": "Analysis", "artifact": "Artifact", "run": "Graph run", "branch": "Branch", "rule": "Learned rule",
    "business_unit": "Business unit", "team": "Team", "person": "Person", "system": "System", "process": "Process",
    "objective": "Objective", "planner": "Planner", "builder": "Builder", "checker": "Checker", "router": "Gate · Router",
    "merge": "Merge (code)", "done": "Done", "blocked": "Blocked", "approval": "Your approval",
}
STATUS = {"planned": ("Planned", "off"), "running": ("Running", "run"), "done": ("Done", "ok"),
          "partial": ("Partly done", "warn"), "cancelled": ("Cancelled", "off"), "passed": ("Passed", "ok"),
          "blocked": ("Blocked", "bad"), "skipped": ("Skipped", "warn"), "queued": ("Queued", "run"),
          "waiting": ("Waiting for input", "run"), "building": ("Building", "run"),
          "awaiting_approval": ("Needs approval", "wait")}
LANES = {"contained": "Contained · opens on its checks", "wide": "Wide · also needs a clean trajectory",
         "closed": "Closed · opens only with a person's approval"}


def _ctx():
    return client(), workspace()


def palette(mode: str) -> dict[str, Any]:
    """Theme tokens for the canvas. Accent marks metrics, neutral marks datasets and documents, and the status colours
    appear only as health rings, never as a kind."""
    c = tokens.colors(mode)
    s = tokens.series(mode)
    mix = tokens._mix
    kinds = {
        "metric": c["accent"], "dataset": c["neutral"], "document": mix(c["neutral"], c["bg"], .35),
        "term": s[3], "memory": mix(s[3], c["bg"], .4), "analysis": s[5], "artifact": c["link"],
        "run": c["text"], "branch": mix(c["text"], c["bg"], .45), "rule": s[2],
        "planner": c["accent"], "builder": s[3], "checker": s[5], "router": c["text"], "merge": c["neutral"],
        "done": c["neutral"], "blocked": c["neutral"], "approval": s[2],
    }
    for k in ("business_unit", "team", "person", "system", "process", "objective"):
        kinds[k] = mix(c["neutral"], c["bg"], .5)
    return {"colors": {"bg": c["bg"], "surface": c["surface"], "text": c["text"], "muted": c["text-muted"],
                       "border": c["border"], "accent": c["accent"], "focus": c["focus"], "good": c["good"],
                       "warn": c["warn"], "bad": c["bad"], "info": c["info"], "neutral": c["neutral"],
                       "edge": mix(c["border"], c["text"], .25)},
            "kinds": kinds}


def readable(graph: dict[str, Any]) -> dict[str, Any]:
    """Names a person can read on every node: a file Lyst made carries only its id ("art_438165bd1e"), so it takes
    the question of the answer it was made from ("File · Why did operating margin decline last quarter?")."""
    names = {n["id"]: n["label"] for n in graph.get("nodes") or []}
    made = {}
    for lk in graph.get("links") or []:
        if lk.get("rel") == "produced_from" and str(lk.get("source", "")).startswith("artifact:"):
            made[lk["source"]] = names.get(lk["target"])
    for n in graph.get("nodes") or []:
        if n.get("kind") == "artifact" and n.get("label") == str(n.get("id", "")).split(":", 1)[-1]:
            n["label"] = f"File · {made[n['id']]}" if made.get(n["id"]) else "A file Lyst made"
        elif n.get("kind") == "memory" and str(n.get("label", "")).startswith("Q: "):
            n["label"] = n["label"][3:].split(" A: ", 1)[0]  # a memory kept from an answer reads as its question
    return graph


def canvas(graph: dict[str, Any], *, layout: str, key: str, height: int, alt: str,
           selected: str | None = None) -> str | None:
    """Draw the graph; return the id of a node clicked since the last rerun, or None."""
    pal = palette(visuals.mode())
    counts: dict[str, int] = {}
    for n in graph["nodes"]:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    legend = [{"kind": k, "label": KIND_LABELS.get(k, k.replace("_", " ").capitalize()), "count": counts[k],
               "color": pal["kinds"].get(k, pal["colors"]["neutral"])}
              for k in sorted(counts, key=lambda k: (-counts[k], k))]
    statuses = {n.get("status") for n in graph["nodes"]} - {None}
    for s, label, tone in (("ok", "Healthy or passed", "good"), ("warn", "Needs attention", "warn"),
                           ("bad", "Blocked or failing", "bad"), ("run", "In progress", "info")):
        if s in statuses:
            legend.append({"kind": None, "label": label, "color": pal["colors"][tone], "ring": True})
    value = _canvas(graph=graph, layout=layout, height=height, alt=alt, selected=selected, legend=legend,
                    kindLabels=KIND_LABELS, colors=pal["colors"], kinds=pal["kinds"], key=key, default=None)
    seen = f"_{key}_ts"
    if value and value.get("ts") != st.session_state.get(seen):
        st.session_state[seen] = value.get("ts")
        return value.get("id")
    return None


# -- the knowledge map ------------------------------------------------------------------------------------------------

def _open(node: dict[str, Any]) -> None:
    """The existing view for a node."""
    kind, ref = node["kind"], node.get("ref") or node["id"].split(":", 1)[-1]
    if kind == "dataset":
        st.switch_page("app_pages/data.py", query_params={"dataset": ref})
    elif kind == "metric":
        _section("definitions", metric=ref)
    elif kind == "analysis" and node.get("task"):
        st.session_state.task_id = node["task"]
        st.switch_page("app_pages/analyst.py", query_params={"task": node["task"]})
    elif kind == "artifact":
        from lyst.ui import artifacts  # noqa: PLC0415

        artifacts.open_artifact({"kind": "artifact", "id": ref})
    elif kind in ("run", "branch"):
        _go("Runs", run=ref)
    elif kind == "rule":
        _go("Harness")
    elif kind in ("document", "memory", "term"):
        _section({"document": "documents", "memory": "memory", "term": "definitions"}[kind])


def _section(name: str, **params: str) -> None:
    """Another Brain section. The section control's own state is dropped (a widget's value cannot be set after it
    is drawn), so the URL decides it on the rerun."""
    st.session_state.pop("sec_view", None)
    st.query_params.update({"view": name, **params})
    st.rerun()


def _go(tab: str, run: str | None = None) -> None:
    """Another Graph tab (and run), applied before those controls are drawn on the next rerun."""
    st.session_state["_bg_next"] = {"tab": tab, "run": run}
    st.rerun()


def _ask(node: dict[str, Any]) -> None:
    api, ws = _ctx()
    label = node["label"]
    q = {"metric": f"What was {label.lower()} last quarter, and what drove the change?",
         "dataset": f"Summarise what the {label} dataset shows and anything unusual in it.",
         "term": f"What does {label} mean here, and how is it measured?"}.get(node["kind"], f"Tell me about {label}.")
    out = kit.guarded(lambda: api.ask(ws, q))
    if out:
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})


def side_panel(graph: dict[str, Any], nid: str | None) -> None:
    nodes = {n["id"]: n for n in graph["nodes"]}
    node = nodes.get(nid or "")
    if not node:
        kit.label("Selected")
        st.caption("Click a node to see what it is, what links to it and where it comes from. Drag to move, scroll to "
                   "zoom, double-click empty space to fit. **List** shows every node for the keyboard.")
        c = graph.get("counts") or {}
        if c:
            st.html(visuals.stats([(KIND_LABELS.get(k, k), f"{v:,}", None)
                                   for k, v in sorted(c.items(), key=lambda kv: -kv[1])[:4]]))
        return
    st.html(f'<div class="lyst-card-kicker">{kit._esc(KIND_LABELS.get(node["kind"], node["kind"]))}</div>'
            f'<div class="lyst-card-title">{kit._esc(node["label"])}</div>'
            + (f'<div class="lyst-card-meta">{kit._esc(node["detail"])}</div>' if node.get("detail") else ""))
    if node.get("status"):
        kit.chip({"ok": "Healthy", "warn": "Needs attention", "bad": "Blocked", "run": "In progress"}
                 .get(node["status"], node["status"]), {"run": "run"}.get(node["status"], node["status"]))
    inbound = [(lk["rel"], nodes[lk["source"]]) for lk in graph["links"] if lk["target"] == node["id"]]
    outbound = [(lk["rel"], nodes[lk["target"]]) for lk in graph["links"] if lk["source"] == node["id"]]
    for title, items in (("Backlinks", inbound), ("Links to", outbound)):
        if not items:
            continue
        kit.label(f"{title} · {len(items)}")
        for i, (rel, other) in enumerate(items[:12]):
            if st.button(f"{other['label']}", key=f"bgl_{title}_{i}_{other['id']}", type="tertiary",
                         help=f"{rel} · {KIND_LABELS.get(other['kind'], other['kind'])}", icon=":material/link:"):
                st.session_state["bg_sel"] = other["id"]
                st.rerun()
        if len(items) > 12:
            st.caption(f"and {len(items) - 12} more")
    if not inbound and not outbound:
        st.caption("Nothing links here yet.")
    c = st.columns(2)
    if node["kind"] not in ("planner",) and c[0].button("Open", type="primary", icon=":material/arrow_outward:",
                                                        key="bg_open", width="stretch"):
        _open(node)
    if node["kind"] in ("metric", "dataset", "term", "document", "memory", "team", "system", "process", "objective",
                        "business_unit") and c[1].button("Ask", icon=":material/chat:", key="bg_ask", width="stretch"):
        _ask(node)


def knowledge() -> None:
    api, ws = _ctx()
    top = st.columns([3, 1.6], vertical_alignment="center")
    top[0].caption("Every dataset, approved metric, term, document, memory, analysis and artifact, linked only where "
                   "Lyst recorded a relation or a literal mention. Graph runs and the rules they learned join the map.")
    runs = top[1].toggle("Show graph runs", value=True, key="bg_runs")
    try:
        graph = readable(api.get(f"/v1/workspaces/{ws}/brain/graph", runs=runs))
    except APIError as e:
        st.error(kit.reason(e), icon=":material/cloud_off:")
        return
    if not graph["nodes"]:
        kit.empty(":material/hub:", "Nothing in the Brain yet", "Add data, approve a metric or teach Lyst something, "
                  "and it appears here with everything it connects to.")
        return
    left, right = st.columns([3.3, 1.25], gap="medium")
    with left:
        counts = ", ".join(f"{v} {KIND_LABELS.get(k, k).lower()}{'s' * (v != 1)}" for k, v in graph["counts"].items())
        clicked = canvas(graph, layout="force", key="bg_map", height=640, selected=st.session_state.get("bg_sel"),
                         alt=f"Map of the Brain: {counts}; {len(graph['links'])} links. Use List for a keyboard view.")
        if clicked:
            st.session_state["bg_sel"] = clicked
    with right, kit.card("bg_side"):
        side_panel(graph, st.session_state.get("bg_sel"))


# -- graph runs -------------------------------------------------------------------------------------------------------

def _plan_form(*, expanded: bool) -> None:
    api, ws = _ctx()
    with st.expander("Plan a graph run", icon=":material/account_tree:", expanded=expanded):
        st.caption("Write the goal, one job per line. Jobs run at the same time unless one names another's answer "
                   "(write {b1} for branch 1's answer). Start a line with \"optional:\" if the run may skip it. The "
                   "plan is drawn first and waits for your go.")
        with st.form("bg_plan", border=False):
            goal = st.text_area("Goal", height=110, placeholder="What was revenue last quarter?\n"
                                "Which region had the highest operating margin?\noptional: Explain {b1} for the board")
            a = st.columns(2)
            done = a[0].text_input("What does done mean?", placeholder="Every answer verified against the data")
            proof = a[1].text_input("What proves it? (phrases each answer must mention)", placeholder="Q3")
            b = st.columns(2)
            never = b[0].text_input("What must never be touched?", placeholder="payroll")
            approval = b[1].text_input("What needs your approval?", placeholder="board, customers")
            c = st.columns(2)
            tries = c[0].slider("Attempts per branch", 1, 5, 3)
            budget = c[1].number_input("Budget (minutes)", 5, 1440, 120, step=5)
            if st.form_submit_button("Draw the plan", type="primary") and goal.strip():
                run = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs", {
                    "goal": goal, "max_attempts": tries, "budget_minutes": int(budget),
                    "intake": {"done": done or None, "proof": [p.strip() for p in proof.split(",") if p.strip()],
                               "never": never or None, "approval": approval or None}}), "Plan drawn: review it, then go")
                if run:
                    _go("Runs", run=run["id"])


def _branch(run: dict[str, Any], b: dict[str, Any], i: int) -> None:
    api, ws = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    label, tone = STATUS.get(b["status"], (b["status"], "info"))
    meta = (f"{b['id']} · {LANES.get(b['lane'], b['lane'])} · try {b['attempts']} of {run['max_attempts']}"
            + (f" · reads {', '.join(b['after'])}" if b["after"] else "") + (" · optional" if b["optional"] else ""))
    acts = []
    if b["status"] == "awaiting_approval" and b.get("hold") in ("start", "accept") and admin:
        acts = [("Approve", "ok"), ("Reject", "no")]
    hit = kit.row(b["title"], meta, status=(label, tone), actions=acts, key=f"bgb{run['id']}{b['id']}",
                  divider=False)
    if hit:
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs/{run['id']}/branches/{b['id']}/decide",
                                        {"approve": hit == "ok"}), "Decided"):
            st.rerun()
    if b.get("warning"):
        st.caption(md(":material/school: " + b["warning"]))
    if b.get("last_reason") and b["status"] != "passed":
        st.caption(md(":material/report: " + b["last_reason"]))
    if b["status"] == "blocked":
        with st.popover("Fix this branch", icon=":material/build:"):
            st.caption("A new job for this branch only. Its checks stay as planned; if the fix passes, the Planner uses "
                       "it in place of the old job from now on.")
            job = st.text_input("New job", value=b["job"], key=f"bgfix{run['id']}{b['id']}")
            if st.button("Send back to its Builder", type="primary", key=f"bgfixgo{run['id']}{b['id']}"):
                if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs/{run['id']}/branches/{b['id']}/revise",
                                                {"job": job}), "Sent back"):
                    st.rerun()
    if b.get("evidence"):
        with st.expander(f"Checks · {sum(1 for r in b['evidence'] if r['passed'])} of {len(b['evidence'])} pass",
                         icon=":material/fact_check:"):
            for r in b["evidence"]:
                st.markdown(md(f"{':material/check_circle:' if r['passed'] else ':material/cancel:'} **{r['text']}** "
                               f"· {r.get('strength', '')}  \n{r['evidence']}"))
            for a in b.get("history") or []:
                if a.get("packet") and a["outcome"] == "fail":
                    p = a["packet"]
                    st.caption(md(f"Attempt {a['attempt']} returned · **{p.get('policy', 'repair')}** · {p['reason']} · "
                                  f"scope: {p['scope']}"))
    if i < len(run["branches"]) - 1:
        st.divider()


def run_view(run_id: str) -> None:
    api, ws = _ctx()
    try:
        run = api.get(f"/v1/workspaces/{ws}/graph-runs/{run_id}")
    except APIError as e:
        st.error(kit.reason(e), icon=":material/error:")
        return
    label, tone = STATUS.get(run["status"], (run["status"], "info"))
    head = st.columns([4, 1.1, 1.1, 1.1], vertical_alignment="center")
    head[0].markdown(f"**{kit._esc(run['title'])}**")
    kit.chip(label, tone, head[0])
    if run["status"] == "planned" and head[1].button("Go", type="primary", icon=":material/play_arrow:", key="bg_go",
                                                     width="stretch"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs/{run_id}/go"), "Running"):
            st.rerun()
    if run["status"] in ("planned", "running") and head[2].button("Cancel", icon=":material/stop:", key="bg_cancel",
                                                                  width="stretch"):
        if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs/{run_id}/cancel"), "Cancelled"):
            st.rerun()
    if run["status"] not in ("planned",) and head[3].button("Run again", icon=":material/replay:", key="bg_rerun",
                                                            width="stretch", help="Same graph, today's learned rules"):
        new = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/graph-runs/{run_id}/rerun"), "Planned again")
        if new:
            _go("Runs", run=new["id"])
    if run["status"] == "running" and head[1].button("Refresh", icon=":material/refresh:", key="bg_refresh",
                                                     width="stretch"):
        st.rerun()
    flow = run["graph"]
    passed = sum(1 for b in run["branches"] if b["status"] == "passed")
    alt = (f"Flow of {run['title']}: Planner, {len(run['branches'])} Builders with their Checkers, the gate and "
           f"Router, Merge; {passed} passed, {sum(1 for b in run['branches'] if b['status'] == 'blocked')} blocked.")
    with kit.card("bg_flow"):
        hit = canvas(flow, layout="flow", key=f"bg_flow_{run_id}", height=max(380, 150 + 96 * len(run["branches"])),
                     alt=alt)
        if hit:
            node = next((n for n in flow["nodes"] if n["id"] == hit), None)
            if node:
                ct = node.get("contract")
                st.caption(md(f"**{node['label']}** · {node.get('detail', '')}"
                              + (f"  \nJob: {ct['job']} · in {ct['in']} · out {ct['out']} · fails as {ct['fails_as']}"
                                 if ct else "")))
    kit.label(f"Branches · {len(run['branches'])}")
    for i, b in enumerate(run["branches"]):
        _branch(run, b, i)
    final = run.get("final")
    if final:
        kit.label("Final check")
        timing = final.get("timing") or {}
        st.html(visuals.stats([
            ("Branches passed", final["summary"], None),
            ("Every branch ended", "Yes" if final["every_branch_ended"] else "No", None),
            ("Unproven requirements", f"{len(final['requirements_without_evidence'])}", None),
            ("Wall clock vs a chain", f"{timing['graph_seconds']}s vs {timing['as_a_chain_seconds']}s"
             if timing else "—", None)]))
        if run.get("merged"):
            kit.label("Merged answers (code node)")
            for m in run["merged"]:
                st.markdown(md(f"**{m['title']}** · {m['answer']}  \n"
                               f"{m['proof']} requirements with evidence · task {m['task_id']}"
                               + (f" · same answer as {', '.join(m['also'])}" if m["also"] else "")))
        kit.label("What the graph learned")
        for x in final["learned"]:
            st.markdown(md("- " + x))
    with st.expander("The graph as it ran", icon=":material/timeline:"):
        st.dataframe([{"#": e["seq"], "branch": e["branch_id"], "from": e["src"], "to": e["dst"], "edge": e["edge"],
                       "detail": e["detail"]} for e in run["edges"]], hide_index=True, width="stretch")
        d = st.columns(3)
        d[0].download_button("state.json", json.dumps(run, default=str, indent=2), f"{run_id}-state.json",
                             "application/json", icon=":material/download:", key="bg_dl_state")
        try:
            files = api.get(f"/v1/workspaces/{ws}/graph-runs/{run_id}/files")
        except APIError:
            files = {}
        for col, name in ((d[1], "checks.md"), (d[2], "graph.md")):
            if files.get(name):
                col.download_button(name, files[name], f"{run_id}-{name}", "text/markdown", icon=":material/download:",
                                    key=f"bg_dl_{name}")


def runs() -> None:
    api, ws = _ctx()
    try:
        items = api.get(f"/v1/workspaces/{ws}/graph-runs")
    except APIError as e:
        st.error(kit.reason(e), icon=":material/cloud_off:")
        return
    _plan_form(expanded=not items)
    if not items:
        st.caption("No graph run yet. A run splits a goal into branches, builds each one as its own task, checks it "
                   "against requirements a program can fail, and sends only the failing branch back.")
        return
    ids = [r["id"] for r in items]
    if st.session_state.get("bg_run") not in ids:
        st.session_state["bg_run"] = ids[0]
    names = {r["id"]: f"{r['title']} · {STATUS.get(r['status'], (r['status'],))[0]} · {r['passed']}/{r['branches']}"
             for r in items}
    st.selectbox("Run", ids, format_func=names.get, key="bg_run", label_visibility="collapsed")
    run_view(st.session_state["bg_run"])
    c = st.columns(2)
    with c[0].expander("Morning brief", icon=":material/wb_sunny:"):
        try:
            brief = api.get(f"/v1/workspaces/{ws}/graph/brief")
            st.markdown(md("\n".join(f"- {x}" for x in brief["lines"])))
        except APIError as e:
            st.caption(kit.reason(e))
    with c[1].expander("Weekly review", icon=":material/calendar_month:"):
        try:
            rv = api.get(f"/v1/workspaces/{ws}/graph/review")
            st.markdown(md(f"**{rv['runs']} runs this week.** {rv['one_change']}"))
            for f in rv["failed_most"]:
                st.caption(md(f"Failed most · {f['node']} · {f['failures']}"))
            for m in rv["can_merge"]:
                st.caption(md(f"Can merge · {m}"))
        except APIError as e:
            st.caption(kit.reason(e))


# -- the harness ------------------------------------------------------------------------------------------------------

def harness() -> None:
    api, ws = _ctx()
    admin = st.session_state.get("role") in ("admin", "owner")
    try:
        h = api.get(f"/v1/workspaces/{ws}/graph/harness")
    except APIError as e:
        st.error(kit.reason(e), icon=":material/cloud_off:")
        return
    st.caption("What every node re-reads before it works. The fixed rules are enforced in code; the learned rules came "
               "from runs in this workspace.")
    learned = [r for r in h["learned"] if r["active"]]
    kit.label(f"Learned rules · {len(learned)}")
    if not learned:
        st.caption("None yet. A rule is written when the same edge fails twice for the same reason, or when a fix for "
                   "a blocked branch passes.")
    for i, r in enumerate(learned):
        acts = [("Retire", "r")] if admin else []
        tone = ("Avoid", "warn") if r["kind"] == "avoid" else ("Rewrite", "info")
        if kit.row(r["rule"], f"applied {r['hits']} times · learned {kit.when(r['created_at'])}", status=tone,
                   actions=acts, key=f"bgr{r['id']}", divider=i < len(learned) - 1) == "r":
            if kit.guarded(lambda x=r: api.post(f"/v1/workspaces/{ws}/graph/rules/{x['id']}/retire"), "Retired"):
                st.rerun()
    c = st.columns(2)
    with c[0]:
        kit.label("Rules")
        for r in h["fixed"]:
            st.markdown(md("- " + r))
        kit.label("Evidence, strongest first")
        for lvl, txt in h["evidence"]:
            st.markdown(md(f"- **{lvl}** · {txt}"))
        kit.label("Stop rules")
        for r in h["stop_rules"]:
            st.markdown(md(f"- **{r['rule']}** · {r['means']}"))
    with c[1]:
        kit.label("On failure")
        for p in h["policy"]:
            st.markdown(md(f"- {p['when']} → **{p['action']}** · {p['means']}"))
        kit.label("Node contracts")
        for n, ct in h["contracts"].items():
            st.markdown(md(f"- **{n}** · {ct['job']} · in `{ct['in']}` · out `{ct['out']}` · fails as `{ct['fails_as']}`"))
        kit.label("The layers")
        for x in h["layers"]:
            st.markdown(md(f"- **{x['layer']}** ({x['controls']}) · {x['in_lyst']}"))
    st.download_button("Harness as Markdown", h["markdown"], "graph-harness.md", "text/markdown",
                       icon=":material/download:", key="bg_dl_harness")


def view() -> None:
    """Brain › Graph."""
    tabs = ["Map", "Runs", "Harness"]
    nxt = st.session_state.pop("_bg_next", None)
    if nxt:
        st.session_state["bg_tab"] = nxt["tab"]
        if nxt.get("run"):
            st.session_state["bg_run"] = nxt["run"]
    st.session_state.setdefault("bg_tab", "Map")
    tab = st.segmented_control("Graph view", tabs, key="bg_tab", label_visibility="collapsed") or "Map"
    {"Map": knowledge, "Runs": runs, "Harness": harness}[tab]()
