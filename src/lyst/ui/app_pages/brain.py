# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain: what Lyst knows about this business and what it is doing with it. Work (running work, what needs you,
what repeats), the graph, definitions, documents, memory and specializations, each with provenance."""

import pandas as pd
import streamlit as st

from lyst.semantic import MetricSpec, value_expr
from lyst.ui import agent, brain_graph, kit, specialists, visuals, work, zone
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
admin = st.session_state.get("role") in ("admin", "owner")
editor = st.session_state.get("role") in ("editor", "admin", "owner")
TRUST = {"approved": ("Approved", "ok"), "user_validated": ("Validated", "ok"), "observed": ("Observed", "off")}


def ask(question: str) -> None:
    out = kit.guarded(lambda: api.ask(ws, question))
    if out:
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})


@st.dialog("Definition", width="large")
def metric_detail(m: dict, terms: list[dict]) -> None:
    st.markdown(f"**{m['title']}**")
    st.markdown(md(m.get("description") or "No description yet."))
    p = visuals.pulse(api, ws).get(m["name"])
    if p:
        top = st.columns([1.2, 3], vertical_alignment="center")
        top[0].html(f'<div class="lyst-card-kicker">{p["period"]}</div><div class="lyst-card-value lyst-num">'
                    f'{p["value_text"]}</div>{visuals.delta(p["change_text"] + " vs prior " + p["grain"], p.get("good"))}')
        top[1].html(visuals.spark(p["history"], label=f"{m['title']}, last {len(p['history'])} {p['grain']}s",
                                  height=64, width=420))
    alias = sorted({*m.get("synonyms", []), *[t["term"] for t in terms if t.get("maps_to_metric") == m["name"]]})
    dims = ", ".join(d.replace("_", " ") for d in m["dimensions"])
    st.caption((f"Can be split by {dims}" if dims else "Not split by anything")
               + (f" · also called {', '.join(alias)}" if alias else ""))
    with st.expander("How it's calculated", icon=":material/functions:"):
        st.caption(f"From the {m['dataset']} dataset, by {m['time_column']}; referred to as {m['name']}.")
        st.code(value_expr(MetricSpec.model_validate(m)), language="sql")
    lin = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/metrics/{m['name']}/lineage"))
    if lin:
        src = lin.get("dataset") or {}
        fresh = src.get("freshness") or {}
        origin = src.get("origin") or {}
        kit.label("Where the number comes from")
        steps = []
        if origin:
            steps.append(("Source", str(origin.get("name") or origin.get("table") or origin.get("connection")),
                          str(origin.get("kind") or "")))
        steps.append(("Dataset", src.get("title", m["dataset"]), f"{src.get('rows', 0):,} rows"
                      + (f" · through {fresh['latest']}" if fresh.get("latest") else "")))
        steps.append(("Approved metric", m["title"], f"v{lin.get('version', 1)}"))
        st.html(visuals.flow(steps, label=f"Lineage of {m['title']}"))
        st.markdown(md(f"{src.get('title', m['dataset'])}: {src.get('rows', 0):,} rows"
                       + (f", data through {fresh['latest']}" if fresh.get("latest") else "")
                       + (f", loaded from {origin.get('name') or origin.get('table') or origin.get('connection')}"
                          if origin else "")
                       + ({"pass": "; its data contract is kept", "warn": "; its data contract has warnings",
                           "fail": "; its data contract is broken", "breaking": "; its data contract is broken"}
                          .get((lin.get("contract") or {}).get("outcome"), "") if lin.get("contract") else "")
                       + "."))
        for qn in lin.get("quality") or []:
            st.warning(md(qn.get("text") if isinstance(qn, dict) else qn), icon=":material/report:")
        if st.button("Open the dataset", type="tertiary", icon=":material/database:", key=f"mds_{m['name']}"):
            st.switch_page("app_pages/data.py", query_params={"dataset": src.get("name", m["dataset"])})
    hist = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/metrics/{m['name']}/history")) or []
    if hist:
        with st.expander(f"Versions · {len(hist)}", icon=":material/history:"):
            for h in hist:
                st.markdown(md(f"**v{h['version']}** · {kit.when(h['created_at'])} · {h.get('summary') or ''}"
                               + (f" ({h['note']})" if h.get("note") else "")))
                for ch in h.get("changes") or []:
                    st.caption(md(ch if isinstance(ch, str) else ch.get("text", str(ch))))
    if admin:
        with st.popover("Retire this definition", icon=":material/archive:"):
            st.caption("Requests that name it go to its replacement, or are told it was retired.")
            others = [x["name"] for x in api.get(f"/v1/workspaces/{ws}/metrics") if x["name"] != m["name"]]
            rep = st.selectbox("Replaced by", [None] + others, format_func=lambda n: "Nothing" if n is None
                               else n.replace("_", " "), key=f"dep_{m['name']}")
            if st.button("Retire it", type="primary", key=f"depgo_{m['name']}"):
                if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/metrics/{m['name']}/deprecate", {"replaced_by": rep}),
                               "Retired"):
                    st.rerun()
    c = st.columns([2, 1.2], vertical_alignment="center")
    if c[0].button(f"Ask about {kit.lower(m['title'])}", type="primary", icon=":material/arrow_outward:"):
        ask(f"What was {kit.lower(m['title'])} last quarter?")
    with c[1]:
        from lyst.ui import actions  # noqa: PLC0415

        actions.menu("metric", m["name"], page="brain", label="More", exclude=("metric.lineage",))
    from lyst.ui import activity  # noqa: PLC0415

    activity.panel("metric", m["name"], key=f"mact_{m['name']}", admin=st.session_state.get("role") in ("admin", "owner"))


def tidy(metrics: list[dict]) -> None:
    """The definitions' own health: ambiguous terms, missing owners or descriptions, undefined words. A business
    reader sees one line; whoever maintains the definitions opens the list with the fix for each."""
    try:
        issues = api.get(f"/v1/workspaces/{ws}/semantic/audit")
    except APIError:
        return
    if not issues:
        st.caption(":material/check_circle: Every definition is unambiguous, owned and described.")
        return
    serious = [x for x in issues if x["severity"] in ("high", "medium")]
    title = (f"{len(serious)} definition issue{'s' * (len(serious) != 1)} can change an answer"
             if serious else f"{len(issues)} small gap{'s' * (len(issues) != 1)} in the definitions")
    with st.expander(title, icon=":material/rule:" if not serious else ":material/warning:"):
        for k, x in enumerate(sorted(issues, key=lambda x: {"high": 0, "medium": 1}.get(x["severity"], 2))):
            tone = {"high": "bad", "medium": "warn"}.get(x["severity"], "off")
            kit.row(x["text"], "Fix: " + x["fix"], status=(x["severity"].capitalize(), tone), key=f"aud{k}",
                    divider=k < len(issues) - 1)


def _formula(m: dict) -> str:
    """How a metric is built, in its own terms: 'sum of revenue', 'sum of cogs ÷ sum of revenue'."""
    def part(p: dict | None) -> str:
        return f"{p.get('agg', 'sum')} of {str(p.get('column', '')).replace('_', ' ')}" if p else ""

    if m.get("numerator") and m.get("denominator"):
        return f"{part(m['numerator'])} ÷ {part(m['denominator'])}"
    return part(m.get("measure") or m.get("numerator")) or "a governed expression"


def metric_cards(metrics: list[dict], terms: list[dict], per_row: int = 3) -> None:
    """Each approved definition as a knowledge card: what it means, how it is built, where it comes from and how it
    can be split. Its numbers are one click away (the definition opens with its trend and lineage), not on the page."""
    datasets = {d["name"]: d.get("title", d["name"]) for d in _datasets()}
    for start in range(0, len(metrics), per_row):
        cols = st.columns(per_row)
        for col, m in zip(cols, metrics[start:start + per_row], strict=False):
            with col, kit.card(f"m_{m['name']}"):
                alias = sorted({*m.get("synonyms", []), *[t["term"] for t in terms if t.get("maps_to_metric") == m["name"]]})
                chips = "".join(f'<span class="lyst-tag">{kit._esc(d.replace("_", " "))}</span>'
                                for d in (m.get("dimensions") or [])[:4])
                st.html(f'<div class="lyst-card-kicker">{kit._esc(m.get("kind", "measure"))} · '
                        f'{kit._esc(datasets.get(m["dataset"], m["dataset"]))}</div>'
                        f'<div class="lyst-card-title">{kit._esc(m["title"])}</div>'
                        f'<div class="lyst-card-meta" style="margin:.2rem 0 .45rem 0">'
                        f'{kit._esc(m.get("description") or "No description yet")}</div>'
                        f'<div class="lyst-formula">{kit._esc(_formula(m))}</div>'
                        + (f'<div class="lyst-tags">{chips}</div>' if chips else "")
                        + (f'<div class="lyst-card-meta">also called {kit._esc(", ".join(alias[:3]))}</div>' if alias else ""))
                if st.button("Open", key=f"m{m['name']}_open", type="tertiary", icon=":material/arrow_outward:") \
                        or st.query_params.get("metric") == m["name"]:
                    st.query_params.pop("metric", None)
                    metric_detail(m, terms)


def _datasets() -> list[dict]:
    try:
        return api.get(f"/v1/workspaces/{ws}/datasets")
    except APIError:
        return []


def definitions() -> None:
    metrics = api.get(f"/v1/workspaces/{ws}/metrics")
    terms = api.get(f"/v1/workspaces/{ws}/glossary")
    tidy(metrics)
    if not metrics:
        kit.empty(":material/menu_book:", "No approved metrics", "A metric is a governed definition (what revenue or margin "
                  "means here). Admins approve them; Lyst answers only from approved definitions.")
    q = st.text_input("Find", placeholder="Find a metric or term", label_visibility="collapsed", key="def_find") \
        if len(metrics) + len(terms) > 8 else ""
    hits = [m for m in metrics if not q or q.lower() in (m["title"] + m["name"] + " ".join(m["synonyms"])).lower()]
    if hits:
        kit.label(f"Metrics · {len(hits)}")  # how they connect is the Graph section's job
        metric_cards(hits, terms)
    tshown = [t for t in terms if not q or q.lower() in (t["term"] + t["definition"]).lower()]
    if tshown:
        kit.label(f"Business terms · {len(tshown)}")
        for i, t in enumerate(tshown):
            kit.row(t["term"], t["definition"] + (f" · means metric {t['maps_to_metric']}" if t.get("maps_to_metric") else ""),
                    key=f"g{i}", divider=i < len(tshown) - 1)
    if admin:
        with st.popover("Add a business term", icon=":material/add:"):
            with st.form("term", border=False):
                term = st.text_input("Term", placeholder="Profitability")
                definition = st.text_input("Meaning", placeholder="Operating margin, by default")
                maps = st.selectbox("Same as metric", [None] + [m["name"] for m in metrics],
                                    format_func=lambda n: "None" if n is None else n.replace("_", " "))
                if st.form_submit_button("Save", type="primary") and term and definition:
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/glossary", {
                            "term": term, "definition": definition, "maps_to_metric": maps}), "Saved"):
                        st.rerun()


def documents() -> None:
    docs = api.get(f"/v1/workspaces/{ws}/documents")
    q = st.text_input("Search", placeholder="Search policies, notes and reports (revenue recognition policy)",
                      label_visibility="collapsed", key="doc_q")
    if q:
        hits = api.get(f"/v1/workspaces/{ws}/documents/search", q=q)
        if not hits:
            st.caption("No passage matches.")
        for h in hits:
            with st.container(border=True):
                st.caption(f"{h['title']} · passage {h['passage']}")
                st.markdown(md(h["text"]))
        return
    if docs:
        st.html(visuals.stats([("Documents", f"{len(docs)}", None),
                               ("Passages Lyst can cite", f"{sum(int(d.get('passages') or 0) for d in docs):,}", None),
                               ("Latest added", kit.when(max(d["added_at"] for d in docs)), None)]))
    if not docs:
        kit.empty(":material/description:", "No documents", "Policies, definitions and notes you add here are cited in "
                  "answers, and disagreements with approved definitions are disclosed.")
    for i, d in enumerate(docs):
        kit.row(d["title"], f"{d['filename']} · {d['passages']} passages · readable by {d['min_role']} and above · "
                f"added {kit.when(d['added_at'])}", key=f"doc{i}", divider=i < len(docs) - 1)
    if editor:
        with st.popover("Add a document", icon=":material/upload_file:"):
            with st.form("doc", border=False):
                f = st.file_uploader("Text, Markdown or PDF", type=["txt", "md", "pdf", "csv"])
                dt = st.text_input("Title (optional)")
                mr = st.selectbox("Who can read it", ["viewer", "editor", "admin", "owner"],
                                  format_func=lambda r: f"{r.capitalize()}s and above")
                if st.form_submit_button("Add", type="primary") and f:
                    r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/documents", None,
                                                     data={"title": dt or f.name, "min_role": mr},
                                                     files={"file": (f.name, f.getvalue())}))
                    if r:
                        st.toast(f"Added {r['title']} ({r['passages']} passages)", icon=":material/check:")
                        st.rerun()


def review_memory() -> None:
    """Admins: memories that may no longer apply (newer data, a changed definition, or age), each with the reason.
    Teach a correction below when one is wrong; Lyst stops using memories that no longer apply."""
    if not admin:
        return
    try:
        stale = api.get(f"/v1/workspaces/{ws}/memory/review")
    except APIError:
        return
    if not stale:
        return
    with kit.group(f"Waiting for review · {len(stale)}", "mem_review",
                   note="Lyst no longer relies on these. Correct any that are wrong with the form below."):
        for i, m in enumerate(stale[:20]):
            kit.row(m["content"], " · ".join(x for x in (m.get("stale_reason"), m.get("layer"), kit.when(m.get("created_at"))) if x),
                    status=("May not apply", "warn"), key=f"mr{m['id']}", divider=i < min(len(stale), 20) - 1)


def memory() -> None:
    review_memory()
    mem = api.get(f"/v1/workspaces/{ws}/memory")
    with st.form("correction", border=True):
        text = st.text_input("Teach Lyst something it should remember",
                             placeholder="Revenue for EMEA excludes intercompany sales.")
        if st.form_submit_button("Remember", type="primary") and text:
            if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/memory/corrections", {"content": text}),
                           "Remembered as validated team knowledge"):
                st.rerun()
    taught = [m for m in mem if m["trust"] in ("approved", "user_validated")]
    learned = [m for m in mem if m["trust"] not in ("approved", "user_validated")]
    if mem:  # how much of what Lyst knows a person stands behind
        n = {t: sum(1 for m in mem if m["trust"] == t) for t in ("approved", "user_validated")}
        st.html(visuals.stacked([(f"Approved {n['approved']}", n["approved"], "good"),
                                 (f"Validated by the team {n['user_validated']}", n["user_validated"], "accent"),
                                 (f"Observed in past analyses {len(learned)}", len(learned), "neutral")],
                                label="What Lyst knows, by how much it is trusted", height=10))
    if taught:
        kit.label(f"Knowledge · {len(taught)}")
        for i, m in enumerate(taught):
            label, tone = TRUST.get(m["trust"], (m["trust"], "info"))
            acts = [("Promote", "p")] if admin and m["trust"] != "approved" else []
            if kit.row(m["content"], f"{m['layer']} · {kit.when(m['created_at'])}", status=(label, tone), actions=acts,
                       key=f"k{m['id']}", divider=i < len(taught) - 1) == "p":
                if kit.guarded(lambda x=m: api.post(f"/v1/workspaces/{ws}/memory/{x['id']}/promote"),
                               "Promoted to organisation knowledge"):
                    st.rerun()
    if learned:
        kit.label(f"From past analyses · {len(learned)}")
        for i, m in enumerate(learned[:15]):
            kit.row(m["content"], kit.when(m["created_at"]), key=f"l{m['id']}", divider=i < min(len(learned), 15) - 1)
    if not mem:
        st.caption("Nothing yet. Lyst records what it learns from verified answers, and what you teach it.")
    if admin:
        with st.expander("Business entities and relations", icon=":material/account_tree:"):
            ents = api.get(f"/v1/workspaces/{ws}/entities")
            if ents:
                st.dataframe(pd.DataFrame(ents)[["kind", "name", "description", "owner"]], hide_index=True, width="stretch")
            with st.form("entity", border=False):
                ec = st.columns([1, 2, 3])
                kind = ec[0].selectbox("Kind", ["business_unit", "team", "person", "system", "process", "objective"],
                                       format_func=lambda k: k.replace("_", " ").capitalize())
                name = ec[1].text_input("Name")
                desc = ec[2].text_input("Description")
                if st.form_submit_button("Add entity") and name:
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/entities",
                                                    {"kind": kind, "name": name, "description": desc}), "Added"):
                        st.rerun()
            with st.form("relation", border=False):
                rc = st.columns(5)
                fk = rc[0].text_input("From kind", "team")
                fn = rc[1].text_input("From name")
                rel = rc[2].text_input("Relation", "owns")
                tk = rc[3].text_input("To kind", "metric")
                tn = rc[4].text_input("To name")
                if st.form_submit_button("Add relation") and fn and tn:
                    if kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/relations", {
                            "from_kind": fk, "from_name": fn, "rel": rel, "to_kind": tk, "to_name": tn}), "Added"):
                        st.rerun()


# Four sections. Knowledge holds what Lyst knows, each kind a sub-view; ?view= keeps naming the leaf (graph,
# definitions, documents, memory), so every older link, the palette and the graph's own links still land.
SECTIONS = {"Overview": ":material/explore:", "Work": ":material/view_kanban:", "Knowledge": ":material/menu_book:",
            "Specializations": ":material/badge:"}
KNOWLEDGE = {"Definitions": "definitions", "Documents": "documents", "Memory": "memory", "Graph": "graph"}


def tabs(default: str) -> tuple[str, str | None]:
    """Brain's sections as icon pills, kept in the URL (?view=) so links, reloads and old /work links land on them.
    Returns (section, knowledge sub-view or None)."""
    names = list(SECTIONS)
    asked = (st.query_params.get("view") or "").lower()
    leaf = next((k for k, v in KNOWLEDGE.items() if v == asked), None)
    pick = "Knowledge" if leaf else next((n for n in names if n.lower() == asked), default)
    waiting = agent.waiting(agent.state(ws))
    with st.container(key="brain_nav"):
        pick = st.segmented_control("view", names, default=pick, key="sec_view", label_visibility="collapsed",
                                    format_func=lambda n: f"{SECTIONS[n]} {n}" + (f" · {waiting}" if n == "Work" and
                                                                                  waiting else "")) or pick
    if pick != "Knowledge":
        if st.query_params.get("view") != pick.lower():
            st.query_params["view"] = pick.lower()
        return pick, None
    leaf = st.segmented_control("Knowledge", list(KNOWLEDGE), default=leaf or "Definitions", key=f"kn_view_{leaf}",
                                label_visibility="collapsed") or leaf or "Definitions"
    if st.query_params.get("view") != KNOWLEDGE[leaf]:
        st.query_params["view"] = KNOWLEDGE[leaf]
    return pick, leaf


KIND_ICON = {"metric": ":material/speed:", "dataset": ":material/table:", "analysis": ":material/chat:",
             "document": ":material/description:", "memory": ":material/neurology:", "term": ":material/sell:"}


def _open_found(hit: dict) -> None:
    kind, name = hit.get("kind"), hit.get("name")
    if kind == "metric":
        st.session_state.pop("sec_view", None)
        st.query_params.update({"view": "definitions", "metric": name})
        st.rerun()
    elif kind == "dataset":
        st.switch_page("app_pages/data.py", query_params={"dataset": name})
    elif kind == "analysis" and str(name).startswith("a_"):
        st.session_state.task_id = name[2:]
        st.switch_page("app_pages/analyst.py", query_params={"task": name[2:]})
    else:
        ask(f"What do we know about {hit.get('title') or name}?")


def find() -> None:
    """One search over what Lyst knows: definitions, datasets, earlier answers and documents."""
    q = st.text_input("Search what Lyst knows", placeholder="Search what Lyst knows (margin, churn, EMEA…)",
                      label_visibility="collapsed", key="brain_q", icon=":material/search:")
    if not q.strip():
        return
    try:
        hits = api.get(f"/v1/workspaces/{ws}/catalog/search", q=q.strip(), limit=8).get("results") or []
    except APIError as e:
        st.caption(str(e))
        return
    if not hits:
        st.caption(f"Nothing here matches “{md(q.strip())}”. Ask Lyst instead, in the bar above.")
        return
    for i, h in enumerate(hits):
        hit = kit.row(h.get("title") or h.get("name"), " · ".join(x for x in (h.get("kind", "").capitalize(),
                                                                          h.get("why")) if x),
                      actions=[("Open", "open"), ("Ask", "ask")], key=f"bq{i}", divider=i < len(hits) - 1)
        if hit == "open":
            _open_found(h)
        elif hit == "ask":
            ask(f"Tell me about {h.get('title') or h.get('name')}")


def _recent_use(n: int = 12) -> tuple[list[str], list[str]]:
    """The measures and expertise Lyst used in this workspace's latest answers, newest first."""
    from lyst.ui import artifacts  # noqa: PLC0415

    measures: list[str] = []
    expertise: list[str] = []
    done = [t for t in agent.state(ws)["tasks"] if t["status"] == "done"][:n]
    for t in done:
        r = artifacts.result(t["id"]) or {}
        sup = r.get("supporting") or {}
        spec = (sup.get("metric") or {}).get("spec") or {}
        if spec.get("title") and spec["title"] not in measures:
            measures.append(spec["title"])
        for d in (sup.get("expertise") or {}).get("disciplines") or []:
            name = str(d.get("name", d.get("id", ""))) if isinstance(d, dict) else str(d)
            if name and name not in expertise:
                expertise.append(name)
    return measures, expertise


def overview() -> None:
    """Brain's default: search, then what Lyst has been using and what it learned lately. Everything else is in
    Knowledge, Work and Specializations."""
    find()
    measures, expertise = _recent_use()
    c = st.columns(2, gap="large")
    with c[0]:
        kit.label("Used lately")
        if measures:
            pick = st.pills("Used lately", measures[:6], label_visibility="collapsed", key="bo_measures",
                            help="Open a definition")
            if pick:
                metrics = {m["title"]: m["name"] for m in api.get(f"/v1/workspaces/{ws}/metrics")}
                if pick in metrics:
                    _open_found({"kind": "metric", "name": metrics[pick]})
        else:
            st.caption("Nothing yet. Ask a question on Home and the definitions it uses show here.")
        if expertise:
            st.caption("Expertise applied: " + md(", ".join(expertise[:5])))
    with c[1]:
        kit.label("Learned lately")
        try:
            mem = api.get(f"/v1/workspaces/{ws}/memory")[:3]
        except APIError:
            mem = []
        for i, m in enumerate(mem):
            kit.row(m["content"], kit.when(m["created_at"]), key=f"bo_m{m['id']}", divider=i < len(mem) - 1)
        if not mem:
            st.caption("Nothing yet. Lyst records what it learns from verified answers, and what you teach it.")


def header() -> None:
    """A calm opening: the name, and one line of what Lyst knows and is doing."""
    k = agent.knowledge(ws)
    s = agent.state(ws)
    _, head, parts = agent.summary(s, agent.waiting(s))
    knows = ", ".join(f"{n:,} {w}" for w, n in (("approved metrics", k["metrics"]), ("datasets", k["datasets"]),
                                                 ("documents", k["documents"]), ("memories", k["memory"])) if n)
    kit.header("Brain", " · ".join(x for x in (f"Knows {knows}" if knows else None, head, *parts[:1]) if x))


try:
    header()
    zone.board("brain")
    if st.query_params.get("metric") and st.query_params.get("view") in (None, "overview", "graph", "work"):
        st.query_params["view"] = "definitions"  # a link to a metric opens its definition, wherever Brain was
        st.session_state.pop("sec_view", None)
    view, leaf = tabs("Overview")
    if view == "Work":  # what Lyst is doing with what it knows: running work, what needs you, what repeats
        work.section(agent.state(ws)["tasks"], agent.waiting(agent.state(ws)))
    elif view == "Knowledge":
        {"Graph": brain_graph.view, "Definitions": definitions, "Documents": documents, "Memory": memory}[leaf]()
    else:
        {"Overview": overview, "Specializations": specialists.catalog}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
