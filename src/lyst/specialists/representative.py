# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Representative tasks with known answers: the evidence behind Level 3 (Operational).

Each scenario is a request a practitioner of the discipline would ask, run end to end on the demo workspace plus the
synthetic samples (``samples.py``), with checks on the *content* of the answer: the planted pattern must be found
(EMEA drives the margin decline; the approval step is the bottleneck; user07 bursts; INV005 and INV010 break). A
scenario passes only when the discipline is among those selected, the task finishes with a verified answer and
every expected fact appears in it.

``scripts/evidence_specializations.py --write`` runs them and records ``evidence.json``; the test suite re-runs them,
so a change that breaks a discipline's representative task fails CI instead of silently keeping its level.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .catalog import load
from .core import AnalystRequest

# profile id -> [(request, facts that must appear in the answer, evidence or tables)]
SCENARIOS: dict[str, list[tuple[str, list[str]]]] = {
    "financial_data_analyst": [("Why did operating margin decline last quarter?", ["operating margin", "EMEA"])],
    "data_analyst": [("What is our revenue by region last quarter?", ["North America", "EMEA", "APAC", "LATAM"])],
    "forecasting_analyst": [("Forecast revenue for the next two quarters", ["80% interval", "backtest"])],
    "business_intelligence_analyst": [("Build a dashboard for weekly revenue by region", ["Built", "verified"])],
    "statistical_analyst": [("Is the correlation between revenue and support tickets significant?", ["detrended", "not causation"])],
    "data_quality_analyst": [("Profile the pnl dataset for data quality", ["396 rows", "7 columns"])],
    "process_mining_analyst": [("Where does our order process get stuck? Use the order events.",
                                ["created → approved", "10 of 60 cases"])],
    "financial_systems_analyst": [("Reconcile ledger erp against ledger bank",
                                   ["do not agree", "1 only in ledger_erp", "1 only in ledger_bank", "INV005"])],
    "crm_analyst": [("Segment our customers into groups", ["3 segments", "support_tickets"])],
    "predictive_analytics_analyst": [("Which customers are likely to churn?", ["AUC", "support_tickets"])],
    "cybersecurity_analyst": [("Analyze the login events for suspicious patterns", ["1 of 12", "user07"])],
    "geospatial_analyst": [("Which stores are more than 300 km from a warehouse?", ["2 of 6", "Freiburg", "Munich"])],
    "network_analyst": [("Which suppliers are connected in the supplier links network?", ["2 connected group", "Acme"])],
}


def _text(task: dict[str, Any]) -> str:
    res = task.get("result") or {}
    parts = [res.get("answer") or "", *(res.get("evidence") or [])]
    for t in res.get("tables") or []:
        parts += [" ".join(str(c) for c in row) for row in t.get("rows") or []]
    return "\n".join(str(p) for p in parts)


def run(lyst, actor, workspace_id: str, ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pid in ids or list(SCENARIOS):
        rows = []
        for request, facts in SCENARIOS[pid]:
            resp = lyst.analyst_core.handle(actor, AnalystRequest(workspace_id=workspace_id, text=request))
            lyst.runtime.drain()
            task = lyst.runtime.get(actor, resp.task_id)
            text = _text(task)
            selected = [r["id"] for r in resp.expertise.get("roles") or []]
            checks = [{"check": "selected", "passed": pid in selected, "detail": ", ".join(selected) or "general"},
                      {"check": "verified answer", "passed": task.get("outcome") == "complete", "detail": str(task.get("outcome"))}]
            checks += [{"check": f"finds: {f}", "passed": f.lower() in text.lower(), "detail": ""} for f in facts]
            rows.append({"request": request, "passed": all(c["passed"] for c in checks), "checks": checks})
        out[pid] = {"passed": all(r["passed"] for r in rows), "scenarios": rows}
    return out


def evidence_entries(results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The ``evidence.json`` content for the scenarios that passed: Level 3 is recorded only for those."""
    version = load().version
    return {pid: {"operational": {"recorded": date.today().isoformat(), "catalog_version": version,
                                  "scenarios": [s["request"] for s in r["scenarios"]]}}
            for pid, r in sorted(results.items()) if r["passed"]}
