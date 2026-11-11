# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Brain: what Lyst knows about this business, as one field to explore, and what it is doing with it (Work).
Definitions, documents, memory, the graph and specializations sit one gesture away, each with provenance."""

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


# -- Brain, as a place -------------------------------------------------------------------------------------------------
# Overview is the field: everything Lyst knows, gathered by collection, its relationships drawn only for the thing
# picked. Work is the other section. The full lists (definitions, documents, memory), the graph with its runs and
# harness, and the specializations sit one gesture away under More; ?view= names them so every older link, the
# palette and the graph's own links still land.

SECTIONS = {"Overview": ":material/blur_on:", "Work": ":material/view_kanban:"}
LEAVES = {"Definitions": ("definitions", ":material/menu_book:"), "Documents": ("documents", ":material/description:"),
          "Memory": ("memory", ":material/neurology:"), "Graph": ("graph", ":material/hub:"),
          "Specializations": ("specializations", ":material/badge:")}
COLLECTIONS = {"metric": "Metrics", "dataset": "Datasets", "term": "Terms", "document": "Documents",
               "memory": "Memories", "analysis": "Answers", "artifact": "Artifacts", "business_unit": "Business units",
               "team": "Teams", "person": "People", "system": "Systems", "process": "Processes",
               "objective": "Objectives"}
INBOUND = {"reads": "read by", "read": "read by", "used": "used in", "mentions": "mentioned in", "joins": "joins",
           "overlaps": "overlaps", "owns": "owned by", "feeds": "fed by", "cites": "cited in"}
TTL = 20  # seconds: the field is re-read at most this often per workspace
_FIELD = None


def _field():
    """Register Brain's field once per process (its code is read from brain_component/)."""
    global _FIELD
    if _FIELD is None:
        from pathlib import Path  # noqa: PLC0415

        from streamlit.components.v2 import component  # noqa: PLC0415

        here = Path(__file__).resolve().parents[1] / "brain_component"
        _FIELD = component("lyst_knowledge", css=(here / "field.css").read_text(encoding="utf-8"),
                           js=(here / "field.js").read_text(encoding="utf-8"))
    return _FIELD


def graph() -> dict:
    """What Lyst knows as nodes and links (graph runs left to Brain › Graph), each relation once."""
    import time  # noqa: PLC0415

    hit = st.session_state.get("_brain_graph")
    if hit and hit[0] == ws and time.monotonic() - hit[1] < TTL:
        return hit[2]
    try:
        g = api.get(f"/v1/workspaces/{ws}/brain/graph", runs=False)
    except APIError:
        g = {"nodes": [], "links": []}
    seen, links = set(), []
    for lk in g.get("links") or []:
        k = (lk["source"], lk["target"], {"read": "reads"}.get(lk["rel"], lk["rel"]))
        if k not in seen:
            seen.add(k)
            links.append({**lk, "rel": k[2]})
    g = {"nodes": [n for n in g.get("nodes") or [] if n["kind"] in COLLECTIONS], "links": links}
    st.session_state["_brain_graph"] = (ws, time.monotonic(), g)
    return g


def short(text: str, n: int = 34) -> str:
    text = str(text or "").replace("Q: ", "").split(" A: ")[0].strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def relations(g: dict, nid: str) -> list[tuple[str, dict]]:
    """A thing's relationships in words, strongest kinds first: ('reads', Monthly P&L), ('read by', Revenue)."""
    nodes = {n["id"]: n for n in g["nodes"]}
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for lk in g["links"]:
        if lk["source"] == nid and lk["target"] in nodes and lk["target"] not in seen:
            out.append((lk["rel"], nodes[lk["target"]]))
            seen.add(lk["target"])
        elif lk["target"] == nid and lk["source"] in nodes and lk["source"] not in seen:
            out.append((INBOUND.get(lk["rel"], lk["rel"]), nodes[lk["source"]]))
            seen.add(lk["source"])
    order = list(COLLECTIONS)
    return sorted(out, key=lambda r: (order.index(r[1]["kind"]), -(r[1].get("degree") or 0)))


def _search(q: str, g: dict) -> tuple[list[str], list[dict]]:
    """What matches a search: the things in the field it lights up, and the catalog's own results."""
    if not q:
        return [], []
    ql = q.lower()
    ids = [n["id"] for n in g["nodes"] if ql in (n["label"] + " " + (n.get("detail") or "")).lower()]
    try:
        found = api.get(f"/v1/workspaces/{ws}/catalog/search", q=q, limit=8).get("results") or []
    except APIError:
        found = []
    for h in found:
        nid = f"{h.get('kind')}:{h.get('name')}"
        if any(n["id"] == nid for n in g["nodes"]) and nid not in ids:
            ids.append(nid)
    return ids, found


def field(g: dict, hits: list[str]) -> None:
    """The field itself. A pick or a collection comes back as an event and is kept in the session."""
    from lyst.ui.brain_graph import KIND_LABELS  # noqa: PLC0415

    counts: dict[str, int] = {}
    for n in g["nodes"]:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    kinds = [k for k in COLLECTIONS if counts.get(k)]
    coll = st.session_state.get("brain_coll") or "all"
    if coll != "all" and coll not in kinds:
        coll = "all"
    sel = st.session_state.get("brain_sel")
    if sel and not any(n["id"] == sel for n in g["nodes"]):
        sel = st.session_state["brain_sel"] = None
    if sel and coll != "all" and not sel.startswith(coll + ":"):
        # followed a link out of the open collection (a dimmed thing at the field's edge, or a relationship in the
        # panel): open the collection it lives in, so it stays picked with its own relationships drawn
        coll = st.session_state["brain_coll"] = next((n["kind"] for n in g["nodes"] if n["id"] == sel), "all")
    ranked = sorted(g["nodes"], key=lambda n: (n["kind"] != "metric", -(n.get("degree") or 0)))
    named = {n["id"] for n in ranked[:10] if n["kind"] in ("metric", "dataset") or (n.get("degree") or 0) >= 3}
    nodes = [{"id": n["id"], "kind": n["kind"], "label": n["label"], "short": short(n["label"]),
              "kindLabel": KIND_LABELS.get(n["kind"], n["kind"]).lower(), "degree": n.get("degree") or 0,
              "status": n.get("status"), "label_always": n["id"] in named}
             for n in sorted(g["nodes"], key=lambda n: (kinds.index(n["kind"]), -(n.get("degree") or 0), n["label"]))]
    alt = "What Lyst knows: " + ", ".join(f"{counts[k]} {COLLECTIONS[k].lower()}" for k in kinds) \
          + f"; {len(g['links'])} relationships. Pick a thing to see what it connects to."
    data = {"nodes": nodes, "links": g["links"], "selected": sel, "collection": coll, "hits": hits, "alt": alt,
            "hint": "Pick anything to see what it connects to" if len(nodes) > 1 else "",
            "collections": [{"id": "all", "label": "Everything", "count": len(nodes)}]
            + [{"id": k, "label": COLLECTIONS[k], "count": counts[k]} for k in kinds]}
    res = _field()(data=data, key="lyst_knowledge", on_action_change=lambda: None)
    ev = getattr(res, "action", None)
    if isinstance(ev, dict) and ev.get("nonce") != st.session_state.get("_brain_nonce"):
        st.session_state["_brain_nonce"] = ev.get("nonce")
        if ev.get("type") == "collection":
            st.session_state["brain_coll"] = coll = str(ev.get("id") or "all")
            if sel and coll != "all" and not sel.startswith(coll + ":"):
                st.session_state["brain_sel"] = None  # the picked thing left the field: put it down
            st.rerun()
        elif ev.get("type") == "pick":
            st.session_state["brain_sel"] = str(ev.get("id") or "") or None
            st.rerun()


def _open(node: dict, terms: list[dict]) -> None:
    """The full record for a thing: a metric's definition opens here; everything else where it lives."""
    if node["kind"] == "metric":
        m = next((x for x in api.get(f"/v1/workspaces/{ws}/metrics") if x["name"] == node.get("ref")), None)
        if m:
            metric_detail(m, terms)
            return
    brain_graph._open(node)


def essence(g: dict, nid: str) -> None:
    """The picked thing: what it is in a sentence, what it relates to (each one a step further), and two moves."""
    from lyst.ui.brain_graph import KIND_LABELS  # noqa: PLC0415

    node = next(n for n in g["nodes"] if n["id"] == nid)
    with st.container(key="brain_essence"):
        top = st.container(horizontal=True, vertical_alignment="top", gap="small")
        top.html(f'<div class="lyst-panel-kicker"><i class="lyst-kdot k-{kit._esc(node["kind"])}"></i>'
                    f'{kit._esc(KIND_LABELS.get(node["kind"], node["kind"]))}</div>'
                    f'<h2 class="lyst-panel-title lyst-brain-name">{kit._esc(short(node["label"], 80))}</h2>')
        if top.button("Close", key="brain_close", type="tertiary", icon=":material/close:", width="content"):
            st.session_state["brain_sel"] = None
            st.rerun()
        p = visuals.pulse(api, ws).get(node.get("ref")) if node["kind"] == "metric" else None
        if p:
            st.html(f'<div class="lyst-brain-value"><span class="lyst-num lyst-brain-big">{kit._esc(p["value_text"])}</span>'
                    f'{visuals.delta(p["change_text"] + " vs prior " + p["grain"], p.get("good"))}</div>'
                    + visuals.spark(p["history"], label=f"{node['label']}, last {len(p['history'])} {p['grain']}s",
                                    height=44, width=320))
        said = node.get("detail") or ""
        if said and said != node["label"] and not said.startswith("Not defined"):
            st.html(f'<p class="lyst-panel-read">{kit._esc(said)}</p>')
        elif said.startswith("Not defined"):
            st.html(f'<p class="lyst-panel-read lyst-brain-gap">{kit._esc(said)}</p>')
        rel = relations(g, nid)
        if rel:
            st.html(f'<p class="lyst-label">Connected to · {len(rel)}</p>')
            with st.container(key="brain_rel"):
                for i, (verb, other) in enumerate(rel[:8]):
                    if st.button(f"{verb.capitalize()} {short(other['label'], 40)}", key=f"brain_rel_{i}",
                                 type="tertiary", help=KIND_LABELS.get(other["kind"], other["kind"])):
                        st.session_state["brain_sel"] = other["id"]
                        st.rerun()
            if len(rel) > 8:
                st.caption(f"and {len(rel) - 8} more")
        else:
            st.caption("Nothing connects here yet.")
        with st.container(horizontal=True, gap="small", key="brain_moves"):
            if node["kind"] in ("metric", "dataset", "term", "document", "memory") and st.button(
                    "Ask about it", key="brain_ask", type="primary", icon=":material/arrow_outward:"):
                brain_graph._ask(node)
            if st.button("Open", key="brain_open", icon=":material/open_in_full:",
                         type="primary" if node["kind"] in ("analysis", "artifact") else "secondary"):
                _open(node, api.get(f"/v1/workspaces/{ws}/glossary"))


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


def lately(g: dict) -> None:
    """With nothing picked: what Lyst learned and used lately, each a way into the field."""
    with st.container(key="brain_lately"):
        st.html('<p class="lyst-label">Lately</p>')
        try:
            mem = api.get(f"/v1/workspaces/{ws}/memory")[:3]
        except APIError:
            mem = []
        ids = {n["id"] for n in g["nodes"]}
        for i, m in enumerate(mem):
            nid = f"memory:{m['id']}"
            if st.button(short(m["content"], 90), key=f"brain_learn_{i}", type="tertiary", icon=":material/neurology:",
                         help=f"Learned {kit.when(m['created_at'])}", disabled=nid not in ids):
                st.session_state["brain_sel"] = nid
                st.rerun()
        measures, expertise = _recent_use()
        if measures:
            pick = st.pills("Used lately", measures[:5], label_visibility="collapsed", key="bo_measures",
                            help="Definitions Lyst used in recent answers")
            if pick:
                names = {n["label"]: n["id"] for n in g["nodes"] if n["kind"] == "metric"}
                st.session_state.pop("bo_measures", None)
                if pick in names:
                    st.session_state["brain_sel"] = names[pick]
                    st.rerun()
        if expertise:
            st.caption("Expertise applied: " + md(", ".join(expertise[:4])))
        if not mem and not measures:
            st.caption("Nothing yet. Lyst learns from verified answers and from what you teach it.")


def overview() -> None:
    """Brain's default: one search, the field, and the picked thing (or what Lyst learned lately) beside it."""
    g = graph()
    q = st.text_input("Search what Lyst knows", placeholder="Search what Lyst knows", label_visibility="collapsed",
                      key="brain_q", icon=":material/search:").strip()
    hits, found = _search(q, g)
    if q and found:
        with st.container(key="brain_found"):
            for i, h in enumerate(found[:5]):
                nid = f"{h.get('kind')}:{h.get('name')}"
                if st.button(h.get("title") or h.get("name"), key=f"bq{i}", type="tertiary",
                             icon=KIND_ICON.get(h.get("kind"), ":material/arrow_outward:"),
                             help=" · ".join(x for x in (str(h.get("kind", "")).capitalize(), h.get("why")) if x)):
                    if any(n["id"] == nid for n in g["nodes"]):
                        st.session_state["brain_sel"] = nid
                        st.rerun()
                    _open_found(h)
    elif q:
        st.caption(f"Nothing here matches “{md(q)}”. Ask Lyst instead, in the bar above.")
    if not g["nodes"]:
        kit.empty(":material/blur_on:", "Nothing known yet", "Add data, approve a metric or teach Lyst something, and "
                  "it appears here with everything it connects to.")
        return
    left, right = st.columns([2.5, 1], gap="large")
    with left:
        field(g, hits)
    with right:
        sel = st.session_state.get("brain_sel")
        if sel and any(n["id"] == sel for n in g["nodes"]):
            essence(g, sel)
        else:
            lately(g)


KIND_ICON = {"metric": ":material/speed:", "dataset": ":material/table:", "analysis": ":material/chat:",
             "document": ":material/description:", "memory": ":material/neurology:", "term": ":material/sell:"}


def _open_found(hit: dict) -> None:
    kind, name = hit.get("kind"), hit.get("name")
    if kind == "metric":
        leaf("definitions", metric=name)
    elif kind == "dataset":
        st.switch_page("app_pages/data.py", query_params={"dataset": name})
    elif kind == "analysis" and str(name).startswith("a_"):
        st.session_state.task_id = name[2:]
        st.switch_page("app_pages/analyst.py", query_params={"task": name[2:]})
    else:
        ask(f"What do we know about {hit.get('title') or name}?")


def leaf(view: str, **params: str) -> None:
    """One of the deeper places (definitions, documents, memory, graph, specializations)."""
    st.session_state.pop("sec_view", None)
    st.query_params.clear()
    st.query_params.update({"view": view, **params})
    st.rerun()


def header(where: str | None) -> str:
    """The name, one line of what Lyst knows and is doing, and the two sections; the rest under More.
    Returns the section (Overview or Work), or the deeper place's name when one is open."""
    k = agent.knowledge(ws)
    s = agent.state(ws)
    waiting = agent.waiting(s)
    _, head, parts = agent.summary(s, waiting)
    knows = ", ".join(f"{n:,} {w if n != 1 else one}" for w, one, n in (
        ("approved metrics", "approved metric", k["metrics"]), ("datasets", "dataset", k["datasets"]),
        ("documents", "document", k["documents"]), ("memories", "memory", k["memory"])) if n)
    line = " · ".join(x for x in (f"Knows {knows}" if knows else None, head, *parts[:1]) if x)
    cols = st.columns([3, 2.4], vertical_alignment="center")
    if where:
        with cols[0]:
            if st.button("Brain", key="brain_back", type="tertiary", icon=":material/arrow_back:"):
                st.session_state.pop("sec_view", None)
                st.query_params.clear()
                st.rerun()
            st.html(f'<div class="lyst-brain"></div><h1 class="lyst-title lyst-brain-title">{kit._esc(where)}</h1>')
        return where
    cols[0].html(f'<div class="lyst-brain"></div><h1 class="lyst-title lyst-brain-title">Brain</h1>'
                 f'<p class="lyst-brain-line">{kit._esc(line)}</p>')
    asked = (st.query_params.get("view") or "").lower()
    pick = next((n for n in SECTIONS if n.lower() == asked), "Overview")
    with cols[1].container(horizontal=True, gap="small", vertical_alignment="center", horizontal_alignment="right",
                           key="brain_tools"):
        with st.container(key="brain_nav", width="content"):
            pick = st.segmented_control("view", list(SECTIONS), default=pick, key="sec_view",
                                        label_visibility="collapsed",
                                        format_func=lambda n: f"{SECTIONS[n]} {n}" + (f" · {waiting}" if n == "Work"
                                                                                      and waiting else "")) or pick
        with st.popover("More", icon=":material/more_horiz:", key="brain_more",
                        help="Definitions, documents, memory, the graph and specializations"):
            for name, (view, icon) in LEAVES.items():
                if st.button(name, key=f"brain_go_{view}", icon=icon, type="tertiary", width="stretch"):
                    leaf(view)
    if st.query_params.get("view") != pick.lower():
        st.query_params["view"] = pick.lower()
    return pick


_CSS = """<style>
.block-container:has(.lyst-brain) {max-width: var(--lyst-canvas-max); padding-top: 3.2rem;}
h1.lyst-brain-title {font-size: 2.6rem; letter-spacing: -.035em; font-weight: 640; margin-bottom: 0;}
p.lyst-brain-line {color: var(--lyst-text-muted); margin: .2rem 0 .6rem 0; font-size: .95rem;}
div.st-key-brain_tools {gap: .5rem;}
div.st-key-brain_nav [role="radiogroup"] {background: transparent; padding: 0; gap: .3rem; flex-wrap: nowrap;}
div.st-key-brain_nav button[data-variant="segmented_control"] {border-radius: var(--lyst-radius-pill) !important;
  padding: 0 .95rem; min-height: 2.4rem; color: var(--lyst-text-muted); white-space: nowrap;}
div.st-key-brain_nav button[data-variant="segmented_control"]:hover {background: var(--lyst-raised); color: var(--lyst-text);}
div.st-key-brain_nav button[aria-checked="true"] {box-shadow: var(--lyst-shadow-2); color: var(--lyst-text) !important;
  background: var(--lyst-raised) !important; font-weight: var(--lyst-weight-medium);}
div.st-key-brain_nav button[aria-checked="true"] [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
div.st-key-brain_more button {border-radius: var(--lyst-radius-pill) !important; min-height: 2.4rem;
  background: transparent !important; box-shadow: none !important; border-color: transparent !important;}
div.st-key-brain_back button {color: var(--lyst-text-muted); padding-left: 0; min-height: 0;}
div.st-key-brain_q [data-testid="stTextInputRootElement"] {border-radius: var(--lyst-radius-pill) !important;
  background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1); border-color: var(--lyst-edge) !important; min-height: 2.8rem;}
div.st-key-brain_q {margin: .6rem 0 .3rem 0;}
div.st-key-brain_found {gap: 0; margin: -.1rem 0 .6rem 0;}
div.st-key-brain_found button {min-height: 0; padding: .3rem .6rem; border-radius: var(--lyst-radius-pill);}
div.st-key-brain_found button:hover {background: var(--lyst-raised); box-shadow: var(--lyst-shadow-1);}
div.st-key-brain_essence, div.st-key-brain_lately {padding: 1.35rem 1.4rem 1.2rem; border-radius: 22px;
  background: var(--lyst-raised); border: 1px solid var(--lyst-edge); box-shadow: var(--lyst-shadow-2); gap: .55rem;}
div.st-key-brain_essence {animation: lyst-sheet var(--lyst-motion-slow) var(--lyst-motion-ease); margin-top: 3.05rem;}
div.st-key-brain_lately {background: transparent; box-shadow: none; border: 0 !important; padding: 0 0 0 .2rem; margin-top: 3.05rem;}
div.st-key-brain_lately button, div.st-key-brain_rel button {justify-content: flex-start; text-align: left; min-height: 0;
  padding: .35rem .55rem; border-radius: 12px; width: 100%; color: var(--lyst-text);}
div.st-key-brain_lately button > div, div.st-key-brain_rel button > div {justify-content: flex-start;}
div.st-key-brain_lately button p, div.st-key-brain_rel button p {font-size: var(--lyst-size-sm); line-height: 1.4;}
div.st-key-brain_lately button:hover, div.st-key-brain_rel button:hover {background: var(--lyst-surface);}
div.st-key-brain_lately [data-testid="stIconMaterial"] {color: var(--lyst-accent);}
div.st-key-brain_rel {gap: 0; margin: -.3rem 0 .2rem -.55rem;}
div.st-key-brain_moves button {border-radius: var(--lyst-radius-pill) !important; min-height: 2.4rem; padding: 0 1rem;}
div.st-key-brain_close button {color: var(--lyst-text-muted); justify-content: flex-end;}
div.st-key-brain_close button p {position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0);}
.lyst-panel-kicker {display: flex; align-items: center; gap: .45rem; font-size: var(--lyst-size-xs);
  color: var(--lyst-text-muted); text-transform: uppercase; letter-spacing: var(--lyst-tracking-label); font-weight: 600;}
h2.lyst-panel-title {font-size: 1.45rem; letter-spacing: -.02em; margin: .25rem 0 0 0; padding: 0; font-weight: 650;
  line-height: 1.25;}
p.lyst-panel-read {font-size: .98rem; line-height: 1.55; margin: 0;}
p.lyst-brain-gap {color: var(--lyst-text-muted);}
.lyst-brain-value {display: flex; align-items: baseline; gap: .7rem; flex-wrap: wrap;}
.lyst-brain-value .lyst-brain-big {font-size: 1.9rem; font-weight: 650; letter-spacing: -.02em;}
i.lyst-kdot {width: .55rem; height: .55rem; border-radius: 50%; background: var(--lyst-neutral); display: inline-block;}
i.lyst-kdot.k-metric {background: var(--lyst-accent);}
i.lyst-kdot.k-dataset {border-radius: 2px;}
i.lyst-kdot.k-term {background: var(--lyst-mark-floor-a); transform: rotate(45deg) scale(.85); border-radius: 1px;}
i.lyst-kdot.k-memory, i.lyst-kdot.k-analysis, i.lyst-kdot.k-document, i.lyst-kdot.k-artifact {background: transparent;
  box-shadow: inset 0 0 0 2px var(--lyst-link);}
@media (max-width: 640px) {
  .block-container:has(.lyst-brain) {padding-top: 4rem;}
  h1.lyst-brain-title {font-size: 2rem;}
  div.st-key-brain_tools {justify-content: flex-start;}
  div.st-key-brain_essence, div.st-key-brain_lately {margin-top: .4rem;}
  div.st-key-brain_essence {padding: 1.1rem 1rem; border-radius: 18px;}
}
</style>"""


try:
    st.html(_CSS)
    if st.query_params.get("metric") and st.query_params.get("view") in (None, "overview", "graph", "work"):
        st.query_params["view"] = "definitions"  # a link to a metric opens its definition, wherever Brain was
    asked = (st.query_params.get("view") or "").lower()
    deeper = next((name for name, (v, _i) in LEAVES.items() if v == asked), None)
    view = header(deeper)
    if deeper:
        {"Graph": brain_graph.view, "Definitions": definitions, "Documents": documents, "Memory": memory,
         "Specializations": specialists.catalog}[deeper]()
    else:
        zone.board("brain")
        if view == "Work":  # what Lyst is doing with what it knows: running work, what needs you, what repeats
            work.section(agent.state(ws)["tasks"], agent.waiting(agent.state(ws)))
        else:
            overview()
            zone.board("brain", place="bottom")  # Work lists the same activity itself; one quiet line would repeat it
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
