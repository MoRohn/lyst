# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Skill discovery: find the approved skill (or built-in capability) that fits an objective, by what it does and
whether it can run here, before anything new is drafted.

Each approved skill declares its purpose, triggers, the SQL it reads (and so the datasets), parameters, its tests and
their last report. Discovery ranks candidates by meaning, not by name: a BM25 score over purpose, triggers, name and
the words of its parameters, with light stemming and a small business-synonym table, so "how concentrated is revenue"
finds a skill whose purpose says "Herfindahl concentration index". A candidate is then checked:

* **compatibility**: every dataset its input reads still exists and the columns it names are still there;
* **permission**: the person may query each of those datasets (the same SQL guard as every query);
* **health**: its last sandbox test report passed;
* **use**: how many times it has run in this workspace, and when last.

Only a compatible, permitted, healthy skill is offered for reuse; the others are listed with the reason, so a person
sees why a skill was not used. Discovery never changes a skill or its permissions.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was", "how", "what", "which", "our",
        "we", "by", "with", "across", "show", "me", "please", "this", "that", "it", "do", "does", "last", "quarter",
        "month", "year", "be", "can", "you", "i", "my", "per", "each", "from", "into", "compute", "calculate"}
SYNONYMS = {
    "concentrated": "concentration", "concentrate": "concentration", "herfindahl": "concentration", "hhi": "concentration",
    "spread": "dispersion", "variability": "dispersion", "volatility": "dispersion", "inequality": "gini",
    "churn": "attrition", "turnover": "attrition", "seasonal": "seasonality", "seasonally": "seasonality",
    "outliers": "anomaly", "outlier": "anomaly", "unusual": "anomaly", "abnormal": "anomaly",
    "pareto": "contribution", "contributors": "contribution", "contributor": "contribution", "share": "contribution",
    "cohort": "cohort", "retention": "retention", "elasticity": "elasticity",
}


_CANON = set(SYNONYMS.values())


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z][a-z0-9]+", (text or "").lower().replace("_", " ")):
        if w in STOP:
            continue
        w = SYNONYMS.get(w, w)
        if w in _CANON:  # canonical terms are kept whole
            out.append(w)
            continue
        for suf in ("ations", "ation", "ness", "ities", "ity", "ing", "ies", "ed", "es", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(SYNONYMS.get(w, w))
    return out


def bm25(query: list[str], docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> list[float]:
    n = len(docs)
    if not n or not query:
        return [0.0] * n
    avg = sum(len(d) for d in docs) / n or 1
    df = {t: sum(1 for d in docs if t in d) for t in set(query)}
    scores = []
    for d in docs:
        s = 0.0
        for t in set(query):
            f = d.count(t)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / avg))
        scores.append(s)
    return scores


def _columns_in(sql: str) -> set[str]:
    return {m.lower() for m in re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', sql)}


def discover(skills_service, registry, actor, ws: str, objective: str, *, limit: int = 5,
             include_capabilities: bool = True) -> dict[str, Any]:
    """Ranked candidates for ``objective`` with the checks that decide whether each may be reused."""
    db = skills_service.stores.workspace(ws)
    rows = db.query("SELECT name, version, purpose, permissions, test_report, approved_by FROM skills WHERE status = 'approved'")
    datasets = {d["name"]: d for d in skills_service.data.catalog(actor, ws)}
    usage = {r["skill"]: r for r in skills_service.stores.control.query(
        "SELECT s.args->>'$.skill' AS skill, count(*) AS runs, max(s.finished_at) AS last_run FROM task_steps s "
        "JOIN tasks t ON t.id = s.task_id WHERE s.capability = 'run_skill' AND s.status = 'done' AND t.workspace_id = ? "
        "GROUP BY 1", [ws])}
    cands: list[dict[str, Any]] = []
    for r in rows:
        man = json.loads(r["permissions"])
        report = json.loads(r["test_report"]) if r["test_report"] else {}
        text = " ".join([r["name"], r["purpose"] or "", " ".join(man.get("triggers", [])), " ".join(man.get("params", {}))])
        reasons = []
        reads = man.get("reads") or []
        for ds in reads:
            if ds not in datasets:
                reasons.append(f"its input dataset {ds} is not available to you here")
        if not reasons:
            cols = set()
            for ds in reads:
                cols |= {c["name"].lower() for c in (datasets[ds].get("schema") or []) if isinstance(c, dict) and "name" in c}
            missing = sorted(c for c in _columns_in(man.get("input_sql", "")) - set(reads) - {"member", "value"}
                             if cols and c not in cols)
            if missing:
                reasons.append(f"its input names column(s) {', '.join(missing)} that the data no longer has")
        if not reasons and not skills_service.guard.can(actor, ws, "data.query"):
            reasons.append("your role cannot query the data it reads")
        cases = report.get("cases") if isinstance(report, dict) else None
        if report.get("static") or (isinstance(cases, list) and any(not c.get("passed", True) for c in cases)):
            reasons.append("its last test report did not pass")
        u = usage.get(r["name"]) or {}
        cands.append({"kind": "skill", "name": r["name"], "version": r["version"], "purpose": r["purpose"],
                      "reads": reads, "triggers": man.get("triggers", []), "runs": int(u.get("runs") or 0),
                      "last_run": u.get("last_run"), "usable": not reasons, "reasons": reasons, "_text": text})
    if include_capabilities:
        for cap in registry.describe():
            if cap["name"] in ("understand", "contextualize", "plan", "validate", "deliver", "learn", "run_skill"):
                continue
            allowed = skills_service.guard.can(actor, ws, cap["permission"])
            cands.append({"kind": "capability", "name": cap["name"], "purpose": cap["description"], "usable": allowed,
                          "reasons": [] if allowed else [f"needs the {cap['permission']} permission"],
                          "_text": f"{cap['name']} {cap['description']}"})
    q = tokens(objective)
    scores = bm25(q, [tokens(c["_text"]) for c in cands])
    top = max(scores) if scores else 0
    for c, s in zip(cands, scores, strict=True):
        c["score"] = round(s, 4)
        c["relevance"] = round(s / top, 3) if top else 0.0
        matched = sorted(set(q) & set(tokens(c.pop("_text"))))
        c["matched_terms"] = matched
    ranked = sorted((c for c in cands if c["score"] > 0), key=lambda c: (-c["score"], c["kind"] != "skill"))[:limit]
    reuse = next((c for c in ranked if c["kind"] == "skill" and c["usable"] and len(c["matched_terms"]) >= 1
                  and c["relevance"] >= 0.999), None)
    return {"objective": objective, "terms": sorted(set(q)), "candidates": ranked,
            "reuse": reuse["name"] if reuse else None,
            "note": "Reuse an approved, compatible skill before drafting a new one." if reuse else
            "No approved skill fits; a new capability can be proposed and tested in the sandbox."}


__all__ = ["bm25", "discover", "tokens"]
