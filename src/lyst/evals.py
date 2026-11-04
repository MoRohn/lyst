# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Evaluation harness: run a bank of real analytical questions and score the answers.

A case states what a correct answer must contain, not how to get it: the status, the metric the
request must resolve to, a value computed independently (pandas on the source data), the driver it
must name, and a latency budget. Runs are stored, so a change to rules, prompts or models can be
compared run against run. The demo bank below is checked in CI; an organization adds its own.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd


@dataclass
class EvalCase:
    id: str
    question: str
    status: str = "complete"
    metric: str | None = None
    value: float | None = None
    value_tolerance: float = 1e-6  # relative
    contains: list[str] = field(default_factory=list)
    driver: str | None = None
    validation: str | None = "passed"
    max_seconds: float = 10.0
    human_baseline_minutes: float | None = None   # a declared estimate of the manual effort, not a measurement
    supervision_minutes: float = 2.0               # time a person spends reading and checking Lyst's answer


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _values_in(result: dict[str, Any]) -> list[float]:
    out: list[float] = []

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, int | float):
            out.append(float(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(result.get("tables", []))
    return out


def score(case: EvalCase, task: dict[str, Any], seconds: float, outputs: dict[str, Any]) -> list[CheckResult]:
    r = task.get("result") or {}
    checks = [CheckResult("finished", task["status"] == "done", f"task {task['status']} {task.get('error') or ''}")]
    checks.append(CheckResult("status", r.get("status") == case.status, f"answer status {r.get('status')}"))
    if case.metric:
        got = ((r.get("supporting") or {}).get("metric") or {}).get("spec", {}).get("name")
        checks.append(CheckResult("metric", got == case.metric, f"resolved to {got}"))
    if case.value is not None:
        cur = (outputs.get("current") or {}).get("value")
        candidates = ([cur] if cur is not None else []) + _values_in(r)
        ok = any(abs(c - case.value) <= abs(case.value) * case.value_tolerance + 1e-9 for c in candidates)
        checks.append(CheckResult("value", ok, f"expected {case.value:.6g}; current {cur}"))
    text = " ".join([r.get("answer", "")] + list(r.get("evidence", [])) + list((r.get("supporting") or {}).get("assumptions", [])))
    for needle in case.contains:
        checks.append(CheckResult(f"contains:{needle}", needle.lower() in text.lower(), "in answer or evidence"))
    if case.driver:
        best = ((outputs.get("drivers") or {}).get("best") or {}).get("member")
        checks.append(CheckResult("driver", str(best) == case.driver, f"named {best}"))
    if case.validation:
        status = (outputs.get("validate") or {}).get("status")
        checks.append(CheckResult("validation", status == case.validation, f"validation {status}"))
    checks.append(CheckResult("latency", seconds <= case.max_seconds, f"{seconds:.2f}s of {case.max_seconds}s"))
    return checks


def run(lyst, actor, workspace_id: str, cases: list[EvalCase], *, label: str = "") -> dict[str, Any]:
    """Run every case through the real runtime and store the scored run."""
    run_id = "ev_" + uuid.uuid4().hex[:10]
    rows = []
    for case in cases:
        start = time.perf_counter()
        tid = lyst.runtime.submit(actor, workspace_id, case.question)
        while lyst.runtime.run_once():
            pass
        seconds = time.perf_counter() - start
        task = lyst.runtime.get(actor, tid)
        outputs = _outputs(lyst, tid)
        checks = score(case, task, seconds, outputs)
        gain = None
        if case.human_baseline_minutes:
            gain = case.human_baseline_minutes / (seconds / 60 + case.supervision_minutes)
        rows.append({"case": case.id, "question": case.question, "task_id": tid, "seconds": round(seconds, 3),
                     "passed": all(c.passed for c in checks), "checks": [asdict(c) for c in checks],
                     "human_baseline_minutes": case.human_baseline_minutes, "supervision_minutes": case.supervision_minutes,
                     "productivity_gain": round(gain, 1) if gain else None})
    passed = sum(r["passed"] for r in rows)
    gains = [r["productivity_gain"] for r in rows if r["productivity_gain"] and r["passed"]]
    summary = {"run_id": run_id, "label": label, "cases": len(rows), "passed": passed,
               "productivity": {"cases_with_baseline": len(gains),
                                "median_gain": float(pd.Series(gains).median()) if gains else None,
                                "note": "Effective gain = declared human baseline ÷ (agent time + declared supervision time), on "
                                        "passing cases only. Baselines are the bank author's estimates, not measurements."},
               "pass_rate": passed / len(rows) if rows else 0.0,
               "p95_seconds": float(pd.Series([r["seconds"] for r in rows]).quantile(0.95)) if rows else 0.0,
               "model": (lyst.llm.info().name if lyst.llm.info() else None), "results": rows}
    lyst.stores.control.execute(
        "INSERT INTO eval_runs (id, workspace_id, label, model, cases, passed, p95_seconds, results, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, workspace_id, label, summary["model"], len(rows), passed, summary["p95_seconds"],
         json.dumps(rows, default=str), actor.user_id])
    return summary


def _outputs(lyst, task_id: str) -> dict[str, Any]:
    """Each finished step's output, by the key the plan gave it."""
    task = lyst.stores.control.query("SELECT plan FROM tasks WHERE id = ?", [task_id])[0]
    keys = ["understand", "contextualize", "plan"] + [s["key"] for s in json.loads(task["plan"] or '{"steps": []}')["steps"]]
    out = {}
    for s in lyst.stores.control.query(
            "SELECT seq, output FROM task_steps WHERE task_id = ? AND status = 'done' ORDER BY seq", [task_id]):
        if s["seq"] < len(keys):
            out[keys[s["seq"]]] = json.loads(s["output"])
    return out


# ------------------------------------------------------------------------------ demo bank


def demo_bank() -> list[EvalCase]:
    """Questions on the Northwind demo, with expected values computed here from the source frame."""
    from . import demo

    df = demo.pnl()
    m = pd.to_datetime(df["month"])

    def between(a, b):
        return df[(m >= a) & (m < b)]

    q3, q2 = between("2026-07-01", "2026-10-01"), between("2026-04-01", "2026-07-01")
    q1_2026 = between("2026-01-01", "2026-04-01")
    y2025 = between("2025-01-01", "2026-01-01")
    margin = lambda d: d.operating_income.sum() / d.revenue.sum()  # noqa: E731
    return [
        EvalCase("margin-why", "Why did operating margin decline last quarter?", metric="operating_margin",
                 value=margin(q3), contains=["Q3 2026", "EMEA"], driver="EMEA", human_baseline_minutes=90),
        EvalCase("margin-synonym", "Why did op margin drop last quarter?", metric="operating_margin",
                 value=margin(q3), driver="EMEA"),
        EvalCase("revenue-last-quarter", "What was our revenue last quarter?", metric="revenue",
                 value=float(q3.revenue.sum()), contains=["Q3 2026"], human_baseline_minutes=15, supervision_minutes=1),
        EvalCase("revenue-named-quarter", "What was revenue in Q1 2026?", metric="revenue",
                 value=float(q1_2026.revenue.sum()), contains=["Q1 2026"]),
        EvalCase("revenue-year", "What was revenue in 2025?", metric="revenue", value=float(y2025.revenue.sum())),
        EvalCase("revenue-by-region", "What was revenue last quarter by region?", metric="revenue",
                 value=float(q3[q3.region == "North America"].revenue.sum()), contains=["North America"]),
        EvalCase("ebit-synonym", "What was EBIT last quarter?", metric="operating_income",
                 value=float(q3.operating_income.sum())),
        EvalCase("hardware-filtered", "What was hardware revenue last quarter?", metric="hardware_revenue",
                 value=float(q3[q3.product_line == "Hardware"].revenue.sum())),
        EvalCase("cogs-why", "Why did the COGS ratio change last quarter?", metric="cogs_ratio",
                 value=q3.cogs.sum() / q3.revenue.sum(), driver="EMEA"),
        EvalCase("ambiguous", "What is our margin?", status="needs_input", contains=["operating_margin", "cogs_ratio"],
                 validation=None),
        EvalCase("unknown-metric", "What was EBITDA last quarter?", status="needs_input",
                 contains=["No approved metric"], validation=None),
        EvalCase("quality", "Profile pnl_ledger for data quality issues", contains=["1 duplicate rows",
                 "revenue: 1 nulls", "cogs: 1 negative values"], validation="passed_with_warnings"),
        EvalCase("forecast", "Forecast revenue", metric="revenue", contains=["backtest"]),
        EvalCase("trend", "Show the operating margin trend over time", metric="operating_margin"),
        EvalCase("deep-investigation", "Investigate our declining operating margins across the last six quarters, "
                 "isolate the strongest contributing factors, test alternative explanations, and prepare an executive "
                 "briefing.", metric="operating_margin", value=margin(q3), driver="EMEA",
                 contains=["favours concentration", "confined to Q3 2026"], max_seconds=15, human_baseline_minutes=240,
                 supervision_minutes=10),
        EvalCase("scenario-c", "Forecast next quarter's support demand, quantify uncertainty, and evaluate three "
                 "staffing scenarios.", metric="tickets", contains=["Q4 2026", "80% interval", "shortfall risk under 5%"],
                 human_baseline_minutes=180, supervision_minutes=10),
        EvalCase("impact-emea", "What was the impact of the supplier price increase on operating margin in EMEA since "
                 "July 2026?", metric="operating_margin", contains=["plausible causal estimate", "Filtered to region = EMEA"]),
        EvalCase("member-filter", "What was revenue in EMEA last quarter?", metric="revenue",
                 value=float(q3[q3.region == "EMEA"].revenue.sum())),
        EvalCase("prior-quarter-value", "What was operating margin in Q2 2026?", metric="operating_margin",
                 value=margin(q2), contains=["Q2 2026"]),
    ]

