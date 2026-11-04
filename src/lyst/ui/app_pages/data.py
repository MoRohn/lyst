# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data: what Lyst reads and what it makes from it. Datasets and their sources, dashboards, saved analyses, SQL."""

import json
import re

import pandas as pd
import streamlit as st

from lyst.ui import kit, onboarding, reports
from lyst.ui.client import APIError, client, workspace
from lyst.ui.components import md

api, ws = client(), workspace()
editor = st.session_state.get("role") in ("editor", "admin", "owner")
EVERY = {None: "Manually", 3600: "Every hour", 86400: "Every day", 604800: "Every week"}


def dataset_name(filename: str) -> str:
    base = re.sub(r"\.(csv|tsv|parquet|txt)$", "", filename.lower())
    return (re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "dataset")[:40]


def ask(question: str) -> None:
    out = kit.guarded(lambda: api.ask(ws, question))
    if out:
        st.session_state.task_id = out["task_id"]
        st.switch_page("app_pages/analyst.py", query_params={"task": out["task_id"]})


@st.cache_data(ttl=3600)
def _formats() -> dict:
    try:
        return api.get("/v1/ingest/formats")
    except APIError:
        return {"extensions": ["csv", "tsv", "parquet"], "groups": {}, "refused": {}}


def upload() -> None:
    """Any file: a workbook, a document, a CSV. Lyst reads it first and shows what it found before anything loads."""
    fm = _formats()
    groups = fm.get("groups") or {}
    st.caption("Spreadsheets (" + ", ".join(groups.get("spreadsheet", [])) + "), tables ("
               + ", ".join(groups.get("table", [])) + ") and documents (" + ", ".join(groups.get("document", []))
               + "). Lyst shows what it found before anything is loaded.")
    f = st.file_uploader("File", type=fm.get("extensions"), label_visibility="collapsed", key="ingest_file")
    jid = st.session_state.get("ingest_job")
    if f is not None:
        seen = st.session_state.get("ingest_seen")
        if seen != (f.name, f.size):  # upload once per file, not on every rerun
            job = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/ingest", None, files={"file": (f.name, f.getvalue())}))
            st.session_state["ingest_seen"] = (f.name, f.size)
            jid = job["id"] if job else None
            st.session_state["ingest_job"] = jid
    if jid:
        review(jid)


def review(jid: str) -> None:
    job = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/ingest/{jid}"))
    if not job:
        st.session_state.pop("ingest_job", None)
        return
    if job.get("status") != "staged":
        st.caption(f"{job['filename']} was already {job['status']}.")
        st.session_state.pop("ingest_job", None)
        return
    st.markdown(md(f"**{job['filename']}** · {job.get('kind', 'file')}"
                   + (f" · {len(job.get('sheets') or [])} sheet{'s' * (len(job.get('sheets') or []) != 1)}"
                      if job.get("sheets") else "")))
    if job.get("duplicate_of"):
        st.info(md(f"This is the same file as an earlier upload ({job['duplicate_of']})."), icon=":material/content_copy:")
    for w in job.get("warnings") or []:
        st.warning(md(w), icon=":material/report:")
    choices = []
    tables = job.get("tables") or []
    if tables:
        kit.label(f"Tables found · {len(tables)}")
    for t in tables:
        with st.container(border=True):
            c = st.columns([0.5, 3, 2], vertical_alignment="center")
            inc = c[0].checkbox("Include", value=t.get("include", True), key=f"inc_{jid}_{t['key']}",
                                label_visibility="collapsed")
            c[1].markdown(md(f"**{t.get('title') or t.get('sheet') or t['key']}**  \n{t['rows']:,} rows · {len(t['columns'])} columns"
                             + (f" · {t['sheet']} {t['range']}" if t.get("sheet") else "")))
            name = c[2].text_input("Dataset name", value=t.get("proposed_name") or dataset_name(t["key"]),
                                   key=f"nm_{jid}_{t['key']}", disabled=not inc)
            for tot in t.get("totals") or []:
                ok = all(ch["matches"] for ch in tot["checks"])
                (st.caption if ok else st.warning)(md(
                    f"“{tot['label']}” row: " + ("matches the sum of the rows, so it is left out of the data" if ok else
                    "does not match: " + "; ".join(f"{ch['column']} says {ch['stated']:,g} but the rows add up to "
                                                   f"{ch['sum_of_rows']:,g}" for ch in tot["checks"] if not ch["matches"]))))
            for w in (t.get("warnings") or []) + (t.get("notes") or []):
                st.caption(":material/info: " + md(w))
            derived = [col["header"] for col in t["columns"] if col.get("formula") or col.get("derived")]
            if derived:
                st.caption("Calculated columns: " + md(", ".join(map(str, derived))))
            with st.expander("Preview", icon=":material/table:"):
                st.dataframe(pd.DataFrame(t.get("preview") or [], columns=[col["header"] for col in t["columns"]]),
                             hide_index=True, width="stretch")
            choices.append({"key": t["key"], "name": name, "include": inc})
    doc = job.get("document")
    searchable = False
    if doc:
        kit.label("Text")
        st.caption(md(f"{doc.get('words', 0):,} words" + (f" · {doc['pages']} pages" if doc.get("pages") else "")
                      + (" · sections: " + ", ".join(doc.get("headings")[:6]) if doc.get("headings") else "")))
        searchable = st.checkbox("Make the text searchable, so answers can cite it", value=doc.get("include", True),
                                 key=f"doc_{jid}")
        with st.expander("Excerpt", icon=":material/article:"):
            st.markdown(md(doc.get("excerpt") or ""))
    b = st.columns([1.6, 1.2, 4])
    if b[0].button("Load it", type="primary", icon=":material/upload:", key=f"commit_{jid}",
                   disabled=not (any(ch["include"] for ch in choices) or searchable)):
        out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/ingest/{jid}/commit",
                                           {"tables": choices, "document": searchable if doc else None}))
        if out:
            made = [f"{d['dataset']} ({d['rows']:,} rows)" for d in out.get("datasets") or []]
            st.toast("Loaded " + (", ".join(made) if made else "")
                     + (" and the text" if out.get("document") else ""), icon=":material/check:")
            for e in out.get("errors") or []:
                st.error(md(e if isinstance(e, str) else e.get("message") or str(e)))
            st.session_state.pop("ingest_job", None)
            st.rerun()
    if b[1].button("Discard", type="tertiary", key=f"discard_{jid}"):
        kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/ingest/{jid}"), "Discarded")
        st.session_state.pop("ingest_job", None)
        st.rerun()


@st.dialog("Add data", width="large")
def add_data() -> None:
    how = st.segmented_control("How", ["Upload a file", "Connect a source"], default="Upload a file",
                               label_visibility="collapsed") or "Upload a file"
    if how == "Upload a file":
        upload()
        return
    kinds = api.get("/v1/connectors")
    kind = st.selectbox("Source", [k["kind"] for k in kinds],
                        format_func=lambda k: next(x["label"] for x in kinds if x["kind"] == k))
    man = next(k for k in kinds if k["kind"] == kind)
    st.caption(man["notes"])
    auth = {"none": "no sign-in", "password": "a user name and password", "token": "an access token",
            "api_key": "an API key"}.get(man.get("auth"), None)
    st.caption(" · ".join(x for x in ("Read-only: Lyst never writes to it" if man.get("read_only") else None,
                                      f"Needs {auth}" if auth else None,
                                      f"Retries up to {man['retry_attempts']} times on a passing failure"
                                      if man.get("retry_attempts") else None) if x))
    with st.form("connection", border=False):
        name = st.text_input("Name", placeholder="Finance warehouse")
        values = {}
        for fld in man["fields"]:
            values[fld["name"]] = st.text_input(fld["label"] + ("" if fld["required"] else " (optional)"),
                                                value=fld["default"] or "", type="password" if fld["secret"] else "default")
        if st.form_submit_button("Test and save", type="primary") and name:
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections", {"kind": kind, "name": name, "values": values}))
            if r:
                st.toast(f"Connected {r['name']}: {r['test']}", icon=":material/check:")
                st.rerun()


SEVERITY = {"high": ("Fix first", "bad"), "medium": ("Worth a look", "warn"), "low": ("Minor", "off")}
OUTCOME = {"pass": ("Contract kept", "ok"), "warn": ("Contract: warnings", "warn"), "fail": ("Contract broken", "bad"),
           "breaking": ("Contract broken", "bad")}


def health(d: dict) -> tuple[str, str] | None:
    """One chip a business reader understands: can I rely on this table?"""
    risks = d.get("risks") or {}
    outcome = (d.get("contract") or {}).get("outcome")
    if outcome in ("fail", "breaking"):
        return OUTCOME[outcome]
    if risks.get("high"):
        return (f"{risks['high']} to fix", "bad")
    if d.get("trusted"):
        return ("Trusted", "verified")
    if risks.get("medium"):
        return (f"{risks['medium']} to check", "warn")
    return None


@st.dialog("Dataset", width="large")
def dataset_detail(d: dict) -> None:
    name = d["name"]
    st.markdown(f"**{d['title']}**")
    fresh = d.get("freshness") or {}
    st.caption(" · ".join(x for x in (d.get("description"), f"{d.get('rows', d.get('row_count', 0)):,} rows",
                                      f"data through {fresh['latest']}" if fresh.get("latest") else None,
                                      f"updated {kit.when(d.get('refreshed_at'))}",
                                      str(d.get("classification", "")).capitalize(),
                                      f"called {name} in SQL") if x))
    view = st.segmented_control("Show", ["Overview", "Preview", "Columns", "Contract"], default="Overview",
                                label_visibility="collapsed", key=f"dsv_{name}") or "Overview"
    if view == "Overview":
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights"))
        if not ins:
            return
        risks = ins.get("risks") or []
        if not risks:
            st.markdown(md(ins.get("summary") or ""))
        kit.label(f"Before you rely on it · {len(risks)}" if risks else "Before you rely on it")
        if not risks:
            st.caption(":material/check_circle: No quality risks found: no duplicate rows at its grain, no gaps in time, "
                       "no unusual values.")
        for k, r in enumerate(risks):
            text, tone = SEVERITY.get(r["severity"], (r["severity"], "info"))
            kit.row(r["text"], ", ".join(c.replace("_", " ") for c in r.get("columns", [])), status=(text, tone),
                    key=f"risk_{name}_{k}", divider=k < len(risks) - 1)
        used = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact")) or []
        kit.label("What depends on it")
        st.caption(", ".join(f"{u['title']} ({u['kind']})" for u in used) if used else "Nothing approved reads it yet.")
        rel = ins.get("relationships") or []
        if rel:
            with st.expander(f"How it connects to other data · {len(rel)}", icon=":material/hub:"):
                st.markdown("\n".join(f"- {md(x['text'])}" for x in rel))
        if st.button("Ask Lyst to check its quality", icon=":material/fact_check:", key=f"askq_{name}"):
            ask(f"Profile {name} for data quality issues")
    elif view == "Preview":
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": f'SELECT * FROM "{name}" LIMIT 50',
                                                                        "max_rows": 50}))
        if r:
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch", height=320)
    elif view == "Columns":
        ins = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/insights"))
        if ins:
            st.dataframe(pd.DataFrame([{"Column": c["name"], "Kind": (c.get("semantic_type") or c.get("role") or "").replace("_", " "),
                                        "Type": c.get("type"), "Missing": c.get("nulls"), "Distinct values": c.get("distinct"),
                                        "Notes": "; ".join(c.get("issues") or [])} for c in ins["columns"]]),
                         hide_index=True, width="stretch",
                         column_config={"Missing": st.column_config.ProgressColumn("Missing", min_value=0, max_value=1)})
            cols = st.multiselect("If these columns changed, what would be affected?", [c["name"] for c in ins["columns"]],
                                  key=f"imp_{name}")
            if cols:
                hits = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/impact", columns=",".join(cols))) or []
                st.markdown("\n".join(f"- **{md(h['title'])}** ({h['kind']}): {md(h['why'])}" for h in hits)
                            if hits else "Nothing approved reads those columns.")
    else:
        contract(name)


def contract(name: str) -> None:
    """A data contract: what this table promises (columns, types, allowed values, key). Every refresh is checked
    against it, and a break reaches the owner's inbox with what it affects."""
    admin = st.session_state.get("role") in ("admin", "owner")
    c = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract"))
    if c is None:
        return
    st.caption("A contract is what this table promises: its columns, their types and allowed values, and what "
               "identifies a row. Lyst checks every refresh against it and tells the owner when one breaks it.")
    if not c.get("active"):
        if not admin:
            st.info("No contract yet. An admin can adopt one Lyst drafts from the data.", icon=":material/handshake:")
            return
        draft = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract/draft"))
        if not draft:
            return
        st.markdown(f"Lyst drafted one from the data: **{len(draft['columns'])} columns**"
                    + (f", one row per {' × '.join(k.replace('_', ' ') for k in draft['key'])}" if draft.get("key") else "")
                    + (f", {len(draft.get('references') or [])} link(s) to other tables" if draft.get("references") else "") + ".")
        st.dataframe(pd.DataFrame([{"Column": x["name"], "Type": x["type"], "Required": x["required"],
                                    "Allowed values": ", ".join(map(str, x["accepted_values"] or [])),
                                    "Minimum": x["min"]} for x in draft["columns"]]), hide_index=True, width="stretch")
        with st.expander("Edit before adopting (JSON)", icon=":material/code:"):
            raw = st.text_area("Contract", json.dumps(draft, indent=1, default=str), height=240, key=f"cdraft_{name}",
                               label_visibility="collapsed")
        if st.button("Adopt this contract", type="primary", icon=":material/handshake:", key=f"cadopt_{name}"):
            try:
                spec = json.loads(raw)
            except ValueError as e:
                st.error(f"The contract is not valid JSON: {e}")
                return
            out = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/datasets/{name}/contract", {"spec": spec}))
            if out:
                text, _ = OUTCOME.get((out.get("check") or {}).get("outcome"), ("Checked", "info"))
                st.toast(f"Contract v{out['version']} adopted · {text}", icon=":material/handshake:")
        return
    status = c.get("status") or {}
    text, tone = OUTCOME.get(status.get("outcome"), ("Not checked yet", "off"))
    top = st.columns([3, 1.4], vertical_alignment="center")
    top[0].markdown(f"**Contract v{status.get('version') or c['active'].get('version', '')}**"
                    + (f" · last checked {kit.when(status.get('checked_at'))}" if status.get("checked_at") else ""))
    kit.chip(text, tone, top[1])
    b = st.columns([1.4, 1.4, 4])
    if b[0].button("Check now", icon=":material/rule:", key=f"ccheck_{name}"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/datasets/{name}/contract/check"))
        if r:
            st.toast(OUTCOME.get(r["outcome"], (r["outcome"], ""))[0], icon=":material/rule:")
    if admin and b[1].button("Retire", type="tertiary", icon=":material/archive:", key=f"cretire_{name}"):
        kit.guarded(lambda: api.delete(f"/v1/workspaces/{ws}/datasets/{name}/contract"), "Contract retired")
    checks = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/datasets/{name}/contract/checks")) or []
    if checks:
        last = checks[0]
        problems = [r for r in last.get("results", []) if r.get("severity") in ("breaking", "fail", "warn")]
        if problems:
            kit.label("What the last check found")
            st.markdown("\n".join(f"- {md(r.get('text') or r.get('detail') or r)}" for r in problems))
            if last.get("impacted"):
                st.caption("Affects " + ", ".join(i.get("title") or i.get("name") for i in last["impacted"]))
        with st.expander(f"Check history · {len(checks)}", icon=":material/history:"):
            for k in checks[:10]:
                st.caption(f"{kit.when(k['checked_at'])} · contract v{k['contract_version']} on data v{k['dataset_version']} · "
                           f"{OUTCOME.get(k['outcome'], (k['outcome'], ''))[0]}")


@st.dialog("Sync a table", width="large")
def sync(conn: dict) -> None:
    try:
        tables = api.get(f"/v1/workspaces/{ws}/connections/{conn['id']}/tables")
    except APIError as e:
        st.error(str(e))
        return
    if not tables:
        st.caption("This source has no tables Lyst can read.")
        return
    tref = st.selectbox("Table", [t["ref"] for t in tables])
    tsel = next(t for t in tables if t["ref"] == tref)
    st.caption("Columns: " + ", ".join(c["name"] for c in tsel["columns"]))
    with st.form("sync", border=False):
        c = st.columns(3)
        dataset = c[0].text_input("Into dataset", value=dataset_name(tref.split(".")[-1]))
        cursor = c[1].selectbox("Only new rows by", [None] + [col["name"] for col in tsel["columns"]],
                                format_func=lambda x: "Reload everything" if x is None else x)
        every = c[2].selectbox("Then", list(EVERY), format_func=EVERY.get)
        if st.form_submit_button("Sync now", type="primary"):
            r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections/{conn['id']}/sync", {
                "source_table": tref, "dataset": dataset, "cursor_column": cursor, "every_seconds": every}))
            if r:
                st.toast(f"{r['dataset']}: {r['rows_added']:,} rows added, {r['rows_total']:,} in total"
                         + ("; the schedule awaits approval" if r.get("schedule") else ""), icon=":material/check:")
                st.rerun()


def datasets(catalog: list[dict]) -> None:
    if not catalog:
        if kit.empty(":material/database:", "No data yet", "Upload a CSV or Parquet file, or connect a database, and "
                     "Lyst can answer questions about it.", action="Add data" if editor else None, key="ds_empty"):
            add_data()
        return
    try:
        info = {c["name"]: c for c in api.get(f"/v1/workspaces/{ws}/catalog")}
    except APIError:
        info = {}
    q = st.text_input("Find", placeholder="Find a dataset", label_visibility="collapsed", key="ds_find") \
        if len(catalog) > 6 else ""
    shown = [d for d in catalog if not q or q.lower() in (d["title"] + d["name"] + (d.get("description") or "")).lower()]
    for i, d in enumerate(shown):
        c = {**d, **info.get(d["name"], {})}
        fresh = (c.get("freshness") or {}).get("latest")
        hit = kit.row(d["title"], " · ".join(x for x in (d.get("description"), f"{d['row_count']:,} rows",
                                                           f"data through {fresh}" if fresh else
                                                           f"updated {kit.when(d['refreshed_at'])}",
                                                           str(d["classification"]).capitalize()) if x),
                      status=health(c), actions=[("Open", "open")], key=f"d{d['name']}", divider=i < len(shown) - 1)
        if hit or st.query_params.get("dataset") == d["name"]:
            st.query_params.pop("dataset", None)
            dataset_detail(c)
STATE = {"healthy": ("Healthy", "ok"), "degraded": ("Degraded", "warn"), "failing": ("Failing", "bad"),
         "needs_action": ("Needs action", "bad"), "untested": ("Not tested", "off")}
CATEGORY = {"auth": "sign-in", "permission": "permissions", "rate_limit": "rate limit", "timeout": "time-out",
            "network": "network", "source": "the source itself", "not_found": "a missing table or path",
            "schema": "a changed layout", "config": "settings", "unknown": "an unknown cause"}
ACTION = {"test": "Test", "discover": "List tables", "read": "Read", "load": "Load", "schema_change": "Layout changed"}


@st.dialog("Source", width="large")
def source_detail(cid: str) -> None:
    """Why a source is healthy or not, what to do about it, what it feeds, and what happened recently."""
    h = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connections/{cid}/health"))
    if not h:
        return
    text, tone = STATE.get(h["state"], (h["state"], "info"))
    top = st.columns([4, 1.4], vertical_alignment="center")
    top[0].markdown(f"**{md(h['name'])}** · {h['kind']}")
    kit.chip(text, tone, top[1])
    if h.get("required_action"):
        st.error(md(h["required_action"]), icon=":material/build:")
    for r in h.get("reasons") or []:
        st.caption(md(r))
    day = h.get("last_24h") or {}
    st.caption(" · ".join(x for x in (
        f"last worked {kit.when(h.get('last_success_at'))}" if h.get("last_success_at") else "has not worked yet",
        f"last failed {kit.when(h.get('last_failure_at'))}" if h.get("last_failure_at") else None,
        f"{h['consecutive_failures']} failures in a row" if h.get("consecutive_failures") else None,
        f"cause: {CATEGORY.get(h['failure_category'], h['failure_category'])}" if h.get("failure_category") else None,
        f"24 h: {day.get('operations', 0)} operations, {day.get('errors', 0)} errors, {day.get('retries', 0)} retries"
        + (f", {day['rate_limited']} rate limited" if day.get("rate_limited") else "")) if x))
    tables = h.get("tables") or []
    kit.label(f"What it feeds · {len(tables)}" if tables else "What it feeds")
    if not tables:
        st.caption("Nothing synced from it yet. Use Sync to load a table as a dataset.")
    for k, t in enumerate(tables):
        sched = t.get("schedule") or {}
        fresh = (t.get("data_freshness") or {}).get("latest")
        con = t.get("contract") or {}
        status = (("Overdue", "warn") if t.get("overdue") else
                  ("Contract broken", "bad") if con.get("outcome") in ("fail", "breaking") else None)
        if kit.row(t["dataset"], " · ".join(x for x in (
                f"from {t['source_table']}", "new rows only" if t.get("mode") == "incremental" else "full reload",
                f"{t.get('rows') or 0:,} rows", f"data through {fresh}" if fresh else None,
                f"synced {kit.when(t.get('last_sync_at'))}" if t.get("last_sync_at") else "never synced",
                (f"repeats {EVERY.get(sched.get('every_seconds'), 'on a schedule').lower()}"
                 + ("" if sched.get("enabled") else " (awaiting approval)")) if sched else None) if x),
                status=status, actions=[("Open", "open")], key=f"st_{cid}_{k}", divider=k < len(tables) - 1):
            st.query_params["dataset"] = t["dataset"]
            st.rerun()
    cred = h.get("credential")
    if cred:
        st.caption(f"Credential v{cred.get('version')} · {cred.get('status')}"
                   + (f" · last used {kit.when(cred.get('last_used_at'))}" if cred.get("last_used_at") else "")
                   + ". Replace or revoke it in Settings › Connections.")
    with st.expander("Recent activity", icon=":material/history:"):
        ev = kit.guarded(lambda: api.get(f"/v1/workspaces/{ws}/connections/{cid}/events", limit=50)) or []
        if not ev:
            st.caption("Nothing recorded yet.")
        else:
            st.dataframe(pd.DataFrame([{
                "When": kit.when(e["occurred_at"]), "What": ACTION.get(e["action"], e["action"]),
                "Table": e.get("source_table") or "", "Outcome": "OK" if e["outcome"] == "ok" else "Error",
                "Cause": CATEGORY.get(e.get("category"), e.get("category") or ""), "Message": e.get("message") or "",
                "Attempts": e.get("attempts"), "Time": f"{e['ms']} ms" if e.get("ms") is not None else ""} for e in ev]),
                hide_index=True, width="stretch", height=min(38 + 35 * len(ev), 360))
    b = st.columns([1.2, 1.2, 4])
    if b[0].button("Test", icon=":material/network_check:", key=f"src_test_{cid}"):
        kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/connections/{cid}/test"), "Connection works")
    if editor and b[1].button("Sync", icon=":material/sync:", key=f"src_sync_{cid}"):
        st.session_state["_sync_after"] = cid
        st.rerun()


def waiting_review() -> None:
    """Uploads Lyst has read but nobody has loaded or discarded yet."""
    try:
        staged = [j for j in api.get(f"/v1/workspaces/{ws}/ingest") if j.get("status") == "staged"]
    except APIError:
        return
    if not staged:
        return
    kit.label(f"Waiting for review · {len(staged)}")
    for i, j in enumerate(staged):
        hit = kit.row(j["filename"], " · ".join(x for x in (j.get("kind"), f"uploaded {kit.when(j.get('created_at'))}")
                                                if x), actions=[("Review", "rev")] if editor else [],
                      key=f"stg{j['id']}", divider=i < len(staged) - 1)
        if hit == "rev":
            st.session_state["ingest_job"] = j["id"]
            add_data()


def connections() -> None:
    conns = api.get(f"/v1/workspaces/{ws}/connections")
    kit.label(f"Sources · {len(conns)}" if conns else "Sources")
    if not conns:
        st.caption("No database or service is connected. Add data › Connect a source links PostgreSQL, SQLite, a REST "
                   "API or a file at a URL; Lyst reads with least privilege and loads tables as versioned datasets.")
        return
    try:
        health = {h["id"]: h for h in api.get(f"/v1/workspaces/{ws}/connections/health")}
    except APIError:
        health = {}
    if (cid := st.session_state.pop("_sync_after", None)) and (c := next((x for x in conns if x["id"] == cid), None)):
        sync(c)
    for i, c in enumerate(conns):
        h = health.get(c["id"]) or {}
        state = STATE.get(h.get("state")) or (("Connected", "ok") if c["status"] == "ok" else (c["status"].capitalize(), "warn"))
        n = len(h.get("tables") or [])
        meta = h.get("required_action") or " · ".join(x for x in (
            c["kind"], f"feeds {n} dataset{'s' * (n != 1)}" if n else None,
            f"last worked {kit.when(h['last_success_at'])}" if h.get("last_success_at") else
            f"tested {kit.when(c.get('last_tested_at')) or 'never'}") if x)
        hit = kit.row(c["name"], meta, status=state, actions=[("Open", "open"), ("Sync", "sync")], key=f"c{c['id']}",
                      divider=i < len(conns) - 1)
        if hit == "open":
            source_detail(c["id"])
        elif hit == "sync":
            sync(c)


def dashboards_view() -> None:
    if editor and st.button("New dashboard", icon=":material/dashboard_customize:", type="tertiary"):
        reports.new_dashboard()
    reports.dashboards()
    reports.saved_reports()


def sql(catalog: list[dict]) -> None:
    st.caption("Read-only SQL against dataset names. Column and row policies apply, and every query is recorded.")
    text = st.text_area("SQL", height=130, label_visibility="collapsed", key="sql_text",
                        value=f'SELECT * FROM {catalog[0]["name"]} LIMIT 50' if catalog else "")
    if st.button("Run", type="primary", icon=":material/play_arrow:"):
        r = kit.guarded(lambda: api.post(f"/v1/workspaces/{ws}/query", {"sql": text, "max_rows": 5000}))
        if r:
            st.caption(f"{len(r['rows']):,} rows · {r['ms']} ms")
            st.dataframe(pd.DataFrame(r["rows"], columns=r["columns"]), hide_index=True, width="stretch")
    mine = [h for h in api.get(f"/v1/workspaces/{ws}/queries", limit=200) if not h.get("task_id")][:8]
    if mine:
        kit.label("Your recent queries")
        for h in mine:
            st.caption(f"{kit.when(h['occurred_at'])} · {h.get('rows') or 0:,} rows · {h['outcome']}")
            st.code(h.get("sql", ""), language="sql")


try:
    catalog = api.get(f"/v1/workspaces/{ws}/datasets")
    if kit.header("Data", "The data Lyst answers from, the dashboards and saved analyses it makes, and a place to "
                  "query it.", action="Add data" if editor else None, icon=":material/add:"):
        add_data()
    view = kit.sections(["Datasets", "Dashboards", "Saved analyses", "SQL"], key="view")
    {"Datasets": lambda: (datasets(catalog), onboarding.data_suggestions(), connections()), "Dashboards": dashboards_view,
    {"Datasets": lambda: (datasets(catalog), waiting_review(), connections()), "Dashboards": dashboards_view,
     "Saved analyses": reports.workflows, "SQL": lambda: sql(catalog)}[view]()
except APIError as e:
    st.error(str(e), icon=":material/cloud_off:")
