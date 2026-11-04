# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain: what Lyst knows about this business. Definitions, documents and memory, each with provenance."""

import pandas as pd
import streamlit as st

from lyst.semantic import MetricSpec, value_expr
from lyst.ui import kit
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
    if st.button(f"Ask about {kit.lower(m['title'])}", type="primary", icon=":material/arrow_outward:"):
        ask(f"What was {kit.lower(m['title'])} last quarter?")


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
        kit.label(f"Metrics · {len(hits)}")
    for i, m in enumerate(hits):
        if kit.row(m["title"], m.get("description") or "No description yet", actions=[("Open", "open")],
                   key=f"m{m['name']}", divider=i < len(hits) - 1) or st.query_params.get("metric") == m["name"]:
            st.query_params.pop("metric", None)
            metric_detail(m, terms)
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


def memory() -> None:
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


try:
    kit.header("Brain", "What Lyst knows about this business, where it came from, and how much to trust it.")
    view = kit.sections(["Definitions", "Documents", "Memory"], key="view")
    {"Definitions": definitions, "Documents": documents, "Memory": memory}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
