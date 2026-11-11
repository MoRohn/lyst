# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Incident and change review: how reliable the service was, how fast it recovered, and which changes broke it.

"What is our mean time to resolve incidents?", "What is our change failure rate?", "Which changes caused incidents?"
and "Incident review for last month" are operational-excellence questions that need durations and links between two
records, not a metric over one. Lyst finds the incidents dataset (an id, an opened time, and usually a resolved time,
a severity and a service) and, when there is one, the changes dataset (an id, an implemented time, usually a service
and an outcome), reads both as the asker, and reports for the period asked about (the last complete month by default):

1. **Incidents**: how many, by severity and service, against the period before, and the services with repeat
   incidents.
2. **Time to restore**: median and mean hours from opened to resolved, by severity and by service, and incidents still
   open with their age.
3. **Changes**: how many, the change failure rate (a change that failed or that an incident is linked to), by service
   and change type, and the incidents each failed change led to.

An incident is linked to a change by the incident's own reference to it (a ``change_id`` or ``caused_by_change``
column). Without one, a change on the same service in the 24 hours before the incident is reported as a *possible*
link, and the answer says it is a time coincidence, not an established cause.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from ..semantic import _q
from . import annotations as ann
from . import brief as brief_mod
from . import periods
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(mttr|mean time to (resolve|restore|recover|repair)|time to (resolve|restore|recover|repair)|"
    r"(incident|outage|post[- ]?incident|change) (review|report|summary|analysis|management)|"
    r"change failure rate|failed changes?|changes? (that )?(caused|broke|led to)|caused (the )?(incidents?|outages?)|"
    r"(incidents?|outages?) (caused|trend|getting (worse|better))|(are|were) (incidents?|outages?)|"
    r"open incidents?|unresolved incidents?|dora|reliability (review|report))\b", re.I)

_ID_INC = ["incident_id", "incident", "incident_number", "ticket_id", "id", "number"]
_ID_CHG = ["change_id", "change", "change_number", "deployment_id", "deploy_id", "release_id", "id", "number"]
_OPENED = ["opened_at", "opened", "created_at", "created", "started_at", "start_time", "detected_at", "reported_at",
           "occurred_at", "date"]
_RESOLVED = ["resolved_at", "resolved", "closed_at", "closed", "restored_at", "ended_at", "end_time", "recovered_at"]
_IMPLEMENTED = ["implemented_at", "deployed_at", "released_at", "completed_at", "changed_at", "start_time",
                "scheduled_at", "created_at", "date"]
_SEVERITY = ["severity", "sev", "priority", "impact", "urgency"]
_SERVICE = ["service", "application", "app", "system", "component", "product", "team", "ci", "configuration_item"]
_OUTCOME = ["outcome", "result", "status", "change_status", "state"]
_TYPE = ["change_type", "type", "category", "kind"]
_LINK = ["caused_by_change", "change_id", "related_change", "linked_change", "cause_change", "root_cause_change"]
_FAILED = re.compile(r"\b(fail\w*|rolled ?back|rollback|backed ?out|unsuccessful|aborted|reverted|caused incident)\b", re.I)
WINDOW_HOURS = 24
_BY_SEV = re.compile(r"(\d+)")


def asks_for_review(text: str) -> bool:
    return bool(ASKS.search(text or "")) and not brief_mod.ELSEWHERE.search(text or "")


def _pick(names: dict[str, str], options: list[str]) -> str | None:
    return next((names[o] for o in options if o in names), None)


def find(datasets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The incidents dataset and, if there is one, the changes dataset, from names and column names only."""
    inc = chg = None
    for d in datasets:
        names = {c["name"].lower(): c["name"] for c in d.get("columns", [])}
        label = f"{d['name']} {d.get('title') or ''}".lower()
        if inc is None and re.search(r"incident|outage|ticket", label):
            opened = _pick(names, _OPENED)
            ident = _pick(names, _ID_INC)
            if opened and ident:
                inc = {"dataset": d["name"], "id": ident, "opened": opened, "resolved": _pick(names, _RESOLVED),
                       "severity": _pick(names, _SEVERITY), "service": _pick(names, _SERVICE),
                       "link": next((names[o] for o in _LINK if o in names and names[o] != ident), None)}
        elif chg is None and re.search(r"change|deploy|release", label):
            at = _pick(names, _IMPLEMENTED)
            ident = _pick(names, _ID_CHG)
            if at and ident:
                chg = {"dataset": d["name"], "id": ident, "at": at, "service": _pick(names, _SERVICE),
                       "outcome": _pick(names, _OUTCOME), "type": _pick(names, _TYPE)}
    if not inc:
        return None
    return {"incidents": inc, "changes": chg}


def understanding(text: str, found: dict[str, Any]) -> dict[str, Any]:
    grain = "quarter" if re.search(r"\b(quarter(ly)?|q[1-4])\b", text, re.I) else "month"
    return {**brief_mod.understanding(text), "intent": "incidents", "grain": grain,
            "dataset": found["incidents"]["dataset"]}


def _read(ctx: Context, spec: dict[str, Any], keys: list[str], evidence: list, purpose: str) -> pd.DataFrame:
    from .engine import _evidence  # noqa: PLC0415

    cols = {k: spec[k] for k in keys if spec.get(k)}
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id,
                                f"SELECT {', '.join(f'{_q(v)} AS {k}' for k, v in cols.items())} FROM {_q(spec['dataset'])}",
                                task_id=ctx.task_id)
    evidence.append(_evidence(r, purpose))
    df = pd.DataFrame(r.rows, columns=list(cols))
    for c in ("opened", "resolved", "at"):
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce", utc=True).dt.tz_localize(None)
    return df


def _hours(s: pd.Series) -> float | None:
    s = s.dropna()
    return float(s.median()) if len(s) else None


def _sev_order(v: Any) -> tuple:
    m = _BY_SEV.search(str(v))
    return (int(m.group(1)) if m else 99, str(v))


@capability(REGISTRY, "incident_review", agent="business_analyst", phase="execute", permission="data.query",
            description="Incidents and changes for a period: counts by severity and service, time to restore, open "
                        "incidents, change failure rate, and the changes linked to incidents.")
def incident_review(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from ..onboarding import fiscal_start  # noqa: PLC0415

    u = ctx.outputs["understand"]
    found = find(ann.metadata(ctx))
    if not found or found["incidents"]["dataset"] != u.get("dataset"):
        return {"empty": True}
    evidence: list[dict[str, Any]] = []
    inc = _read(ctx, found["incidents"], ["id", "opened", "resolved", "severity", "service", "link"], evidence,
                f"{found['incidents']['dataset']}: every incident")
    inc["id"] = inc["id"].astype(str)
    chg = None
    if found["changes"]:
        try:
            chg = _read(ctx, found["changes"], ["id", "at", "service", "outcome", "type"], evidence,
                        f"{found['changes']['dataset']}: every change")
            chg["id"] = chg["id"].astype(str)
        except Exception:  # noqa: BLE001 - changes the asker cannot read are left out and said so
            chg, found["changes"] = None, None
    latest = max(x for x in [inc["opened"].max(), None if chg is None else chg["at"].max()] if pd.notna(x))
    fs = fiscal_start(ctx.services.semantic.stores.workspace(ctx.workspace_id))
    p, note = brief_mod._period(u["question"], u.get("grain") or "month", latest.date(), fs)
    prev = periods.previous(p, fs)

    def within(df: pd.DataFrame, col: str, per: periods.Period) -> pd.DataFrame:
        return df[(df[col] >= pd.Timestamp(per.start)) & (df[col] < pd.Timestamp(per.end))]

    now_inc, prev_inc = within(inc, "opened", p), within(inc, "opened", prev)
    inc["hours"] = (inc["resolved"] - inc["opened"]).dt.total_seconds() / 3600 if "resolved" in inc else None
    cur = inc.loc[now_inc.index]
    out: dict[str, Any] = {"found": found, "period": {"label": p.label, "start": p.start.isoformat(), "end": p.end.isoformat()},
                           "prior_label": prev.label, "note": note, "incidents": len(now_inc), "prior_incidents": len(prev_inc),
                           "evidence": evidence, "latest": latest.date().isoformat()}
    if "severity" in inc:
        out["by_severity"] = sorted(([str(k), int(v), int((prev_inc["severity"] == k).sum())]
                                     for k, v in now_inc["severity"].value_counts().items()), key=lambda r: _sev_order(r[0]))
    if "service" in inc:
        svc = now_inc["service"].value_counts()
        out["by_service"] = [[str(k), int(v), int((prev_inc["service"] == k).sum())] for k, v in svc.items()]
    if "resolved" in inc:
        out["mttr"] = _hours(cur["hours"])
        out["mttr_mean"] = float(cur["hours"].dropna().mean()) if cur["hours"].notna().any() else None
        out["mttr_prior"] = _hours(inc.loc[prev_inc.index, "hours"])
        if "severity" in inc:
            out["mttr_by_severity"] = sorted(([str(k), _hours(g["hours"]), int(g["hours"].notna().sum())]
                                              for k, g in cur.groupby("severity")), key=lambda r: _sev_order(r[0]))
        if "service" in inc:
            out["mttr_by_service"] = sorted(([str(k), _hours(g["hours"]), int(g["hours"].notna().sum())]
                                             for k, g in cur.groupby("service")), key=lambda r: -(r[1] or 0))
        still = inc[inc["resolved"].isna()].copy()
        still["age_days"] = (latest - still["opened"]).dt.total_seconds() / 86400
        out["open"] = [{"id": r.id, "opened": r.opened.date().isoformat(), "age_days": round(float(r.age_days), 1),
                        "severity": None if "severity" not in still else str(r.severity),
                        "service": None if "service" not in still else str(r.service)}
                       for r in still.sort_values("opened").itertuples()][:25]
        out["open_count"] = len(still)
    if chg is not None:
        cur_chg = within(chg, "at", p)
        prev_chg = within(chg, "at", prev)
        failed = cur_chg["outcome"].astype(str).str.contains(_FAILED) if "outcome" in cur_chg else pd.Series(False, index=cur_chg.index)
        links: dict[str, list[str]] = {}
        explicit = "link" in inc and inc["link"].notna().any()
        if explicit:
            for r in now_inc.itertuples():
                if pd.notna(getattr(r, "link", None)) and str(r.link) in set(chg["id"]):
                    links.setdefault(str(r.link), []).append(r.id)
        elif "service" in inc and "service" in chg:
            for r in now_inc.itertuples():
                cand = chg[(chg["service"] == r.service) & (chg["at"] <= r.opened)
                           & (chg["at"] >= r.opened - pd.Timedelta(hours=WINDOW_HOURS))]
                if len(cand):
                    links.setdefault(str(cand.sort_values("at").iloc[-1]["id"]), []).append(r.id)
        linked = cur_chg["id"].isin(links)
        bad = failed | linked
        out["changes"] = {"count": len(cur_chg), "prior": len(prev_chg), "failed": int(failed.sum()), "linked": int(linked.sum()),
                          "failure_rate": float(bad.mean()) if len(cur_chg) else None,
                          "prior_failure_rate": None, "explicit": bool(explicit), "links": links}
        if len(prev_chg):
            pf = prev_chg["outcome"].astype(str).str.contains(_FAILED) if "outcome" in prev_chg else pd.Series(False, index=prev_chg.index)
            plinked = prev_chg["id"].isin(set(inc["link"].dropna().astype(str))) if explicit else pd.Series(False, index=prev_chg.index)
            out["changes"]["prior_failure_rate"] = float((pf | plinked).mean())
        for dim in ("service", "type"):
            if dim in cur_chg:
                g = pd.DataFrame({"k": cur_chg[dim].astype(str), "bad": bad}).groupby("k")["bad"].agg(["count", "mean"])
                out["changes"][f"by_{dim}"] = [[k, int(r["count"]), float(r["mean"])] for k, r in
                                               g.sort_values("mean", ascending=False).iterrows()]
        out["changes"]["caused"] = sorted(([c, len(v), v[:5]] for c, v in links.items()), key=lambda r: -r[1])[:10]
    return out


def _h(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v * 60:.0f} min" if v < 1 else (f"{v:.1f} h" if v < 48 else f"{v / 24:.1f} days")


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, r = o["understand"], o["incidents"]
    if r.get("empty"):
        answer = ("Lyst could not find an incidents dataset it can read here. It looks for one named for incidents or "
                  "outages with an id and an opened time, and a resolved time to measure time to restore.")
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="needs_input")
        out["mode"] = "incidents"
        return out
    p = r["period"]["label"]
    f = r["found"]
    d = r["incidents"] - r["prior_incidents"]
    parts = [f"{r['incidents']} incident{'s' if r['incidents'] != 1 else ''} in {p}, "
             f"{'up' if d > 0 else 'down' if d < 0 else 'level'}{f' {abs(d)}' if d else ''} on {r['prior_label']} "
             f"({r['prior_incidents']})"]
    if r.get("mttr") is not None:
        parts.append(f"median time to restore {_h(r['mttr'])}"
                     + (f" against {_h(r['mttr_prior'])} in {r['prior_label']}" if r.get("mttr_prior") is not None else ""))
    c = r.get("changes")
    if c and c["count"]:
        parts.append(f"{c['count']} changes with a change failure rate of {c['failure_rate']:.0%}"
                     + (f" (against {c['prior_failure_rate']:.0%})" if c.get("prior_failure_rate") is not None else ""))
    answer = "; ".join(parts) + "."
    q = u["question"].lower()
    if c and c["caused"] and re.search(r"caus|broke|led to|failed change|change failure", q):
        top = c["caused"][0]
        lead = (f"{len(c['caused'])} change{'s' if len(c['caused']) != 1 else ''} in {p} "
                f"{'led to' if c['explicit'] else 'were followed within 24 hours by'} incidents; the most was {top[0]} "
                f"({top[1]} incident{'s' if top[1] != 1 else ''}: {', '.join(top[2])}).")
        answer = lead + " " + answer
    elif r.get("open_count"):
        answer += f" {r['open_count']} incident{'s are' if r['open_count'] != 1 else ' is'} still open."
    findings, actions, implications, tables = [], [], [], []
    if r.get("by_severity"):
        findings.append("By severity: " + ", ".join(f"{s} {n} (was {pr})" for s, n, pr in r["by_severity"]) + ".")
        tables.append({"title": f"Incidents by severity, {p}", "columns": ["severity", p, r["prior_label"]],
                       "rows": r["by_severity"]})
        top_sev = r["by_severity"][0]
        if top_sev[1]:
            actions.append(f"Hold a post-incident review for each of the {top_sev[1]} {top_sev[0]} incident"
                           f"{'s' if top_sev[1] != 1 else ''} in {p}, and track its follow-up actions.")
    if r.get("by_service"):
        s0 = r["by_service"][0]
        findings.append(f"Most incidents: {s0[0]} with {s0[1]} (was {s0[2]}).")
        tables.append({"title": f"Incidents by service, {p}", "columns": ["service", p, r["prior_label"]],
                       "rows": r["by_service"]})
        repeat = [s for s in r["by_service"] if s[1] >= 3]
        if repeat:
            implications.append(f"{', '.join(s[0] for s in repeat[:3])} had three or more incidents in {p}; repeats point "
                                "to a shared cause worth a problem record rather than more one-off fixes.")
    if r.get("mttr_by_severity"):
        findings.append("Median time to restore by severity: "
                        + ", ".join(f"{s} {_h(h)} ({n})" for s, h, n in r["mttr_by_severity"]) + ".")
    if r.get("mttr_by_service"):
        tables.append({"title": f"Time to restore by service, {p}", "columns": ["service", "median hours", "resolved"],
                       "rows": [[s, None if h is None else round(h, 2), n] for s, h, n in r["mttr_by_service"]]})
        slow = r["mttr_by_service"][0]
        if slow[1] is not None and r.get("mttr") and slow[1] > 2 * r["mttr"] and slow[2] >= 2:
            actions.append(f"Look at how {slow[0]} incidents are detected and handed over: they take {_h(slow[1])} to "
                           f"restore against {_h(r['mttr'])} overall.")
    if r.get("open"):
        old = [x for x in r["open"] if x["age_days"] > 7]
        findings.append(f"{r['open_count']} incident{'s' if r['open_count'] != 1 else ''} still open"
                        + (f", {len(old)} for more than a week" if old else "") + ".")
        tables.append({"title": "Open incidents, oldest first", "columns": ["incident", "opened", "age (days)", "severity",
                                                                            "service"],
                       "rows": [[x["id"], x["opened"], x["age_days"], x["severity"], x["service"]] for x in r["open"]]})
        if old:
            actions.append(f"Close or re-own the {len(old)} incident{'s' if len(old) != 1 else ''} open for more than a "
                           f"week, oldest {old[0]['id']}.")
    if c:
        if c.get("by_service"):
            worst = c["by_service"][0]
            findings.append("Change failure rate by service: " + ", ".join(f"{k} {m:.0%} of {n}" for k, n, m in c["by_service"]) + ".")
            if worst[2] > (c["failure_rate"] or 0) * 1.5 and worst[1] >= 5:
                actions.append(f"Review how changes to {worst[0]} are tested and approved: {worst[2]:.0%} of them failed "
                               f"or led to an incident, against {c['failure_rate']:.0%} overall.")
            tables.append({"title": f"Change failure rate by service, {p}", "columns": ["service", "changes", "failure rate"],
                           "rows": [[k, n, round(m, 3)] for k, n, m in c["by_service"]]})
        if c.get("by_type"):
            findings.append("By change type: " + ", ".join(f"{k} {m:.0%} of {n}" for k, n, m in c["by_type"]) + ".")
        if c["caused"]:
            tables.append({"title": f"Changes linked to incidents, {p}", "columns": ["change", "incidents", "which"],
                           "rows": [[ch, n, ", ".join(ids)] for ch, n, ids in c["caused"]]})
    checks = [{"name": "numbers_backed", "passed": True, "severity": "error",
               "detail": "every figure comes from the recorded queries of the incident and change data"}]
    if c is not None:
        checks.append({"name": "change_links", "passed": c["explicit"], "severity": "warning",
                       "detail": "incidents name the change that caused them" if c["explicit"] else
                       f"no link column: a change on the same service in the {WINDOW_HOURS} hours before an incident is a "
                       "possible link only"})
    assumptions = [f"Time to restore runs from {f['incidents']['opened']} to {f['incidents']['resolved']}."
                   if f["incidents"]["resolved"] else "There is no resolved time, so time to restore is not measured.",
                   "A change fails when its outcome says failed, rolled back or backed out, or when an incident is linked to it."]
    if r.get("note"):
        assumptions.insert(0, r["note"])
    if not f["changes"]:
        assumptions.append("No changes dataset was found (or it is not readable with your access), so the change failure "
                           "rate is not measured.")
    elif c is not None and not c["explicit"]:
        assumptions.append(f"Links between changes and incidents are a time coincidence on the same service within "
                           f"{WINDOW_HOURS} hours, not an established cause.")
    assumptions.append(f"Data through {r['latest']}; written {datetime.now(UTC):%d %b %Y}.")
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, [], checks,
                           {"evidence": r["evidence"]}, status="complete", assumptions=assumptions)
    out["mode"] = "incidents"
    out["supporting"]["plan"] = [{"capability": "incident_review", "agent": "business_analyst"}]
    out["supporting"]["workflow"]["strategy"] = "incidents"
    out["supporting"]["brief"] = None
    out["supporting"]["incidents"] = {k: r.get(k) for k in ("period", "incidents", "mttr", "open_count")}
    return out


__all__ = ["ASKS", "asks_for_review", "find", "incident_review", "result", "understanding"]
