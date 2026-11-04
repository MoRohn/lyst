# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Analytical workflow compiler: verified analyses become typed, versioned, reusable workflows.

When an analysis finishes *complete and verified* (every error-severity check passed), Lyst compiles
what it did into a workflow definition:

* **Purpose and inputs**: the request pattern with its period lifted out as a typed parameter
  (``"Why did profitability fall in {period}?"``, ``period: quarter``).
* **Understanding**: the resolved intent, metric(s), split, filters and depth, so a repeat does not
  need the model (or the per-dimension member scans) to read the request again.
* **Execution graph**: the plan's steps with their dependencies, the period arguments replaced by
  placeholders (current, prior, year ago) that are re-derived for each run.
* **Data dependencies**: each dataset's schema fingerprint and each metric's approved version.
* **Permissions, validation and budget**: the permissions every step needs, the verification
  intensity, the checks that must pass, and the step budget.
* **Version history and provenance**: the source task, how many verified runs back it, when it was
  last verified, and every earlier version.

Reuse: a request whose wording matches a compiled pattern, or a direct "run workflow X for Q4 2026",
runs the compiled plan. Before every reuse the dependencies are checked: a changed schema or a new
metric version marks the workflow *stale* and Lyst plans afresh, then recompiles a new version from
the next verified run. Nothing unverified is ever compiled, compiled runs are validated exactly like
planned runs, and a compiled run that fails verification does not count as a verified run.

Set ``LYST_WORKFLOWS=0`` to turn compilation and reuse off (used to measure the difference).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

from .security import Guard, Principal

COMPILABLE = {"value", "explain", "breakdown", "trend", "forecast", "correlate", "profile"}
# capabilities a compiled workflow may contain: deterministic reads and analysis, nothing that mutates,
# needs an approval, runs generated code or proposes new capabilities
_NOT_COMPILABLE_CAPS = {"run_skill", "propose_capability", "design_dashboard", "build_dashboard",
                        "refresh_dashboard", "sync_connection", "run_sql"}
_UNDERSTANDING_KEYS = ("intent", "metrics", "matched", "by", "dataset", "filters", "depth", "horizon", "scenarios",
                       "next_period", "second_metric", "audience")
# (pattern, the typed slot it becomes). The slot keeps the grain, so a quarterly workflow never runs for a year.
_PERIOD_PATTERNS = [
    (r"\b(?:in|for|during|over)?\s*q[1-4]\s*(?:fy)?\s*'?\d{2,4}\b", "{quarter}"),
    (r"\b(?:in|for|during|over)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+20\d{2}\b", "{month}"),
    (r"\b(?:in|for|during|over)?\s*(?:the\s+)?(?:last|previous|past|this|current)\s+(quarter|month|year)\b", "{\\1}"),
    (r"\b(?:ytd|year to date)\b", "{ytd}"),
    (r"\b(?:in|for|during|fy)\s*20\d{2}\b", "{year}"),
]


def enabled() -> bool:
    return os.environ.get("LYST_WORKFLOWS", "1") != "0"


def pattern(question: str) -> str:
    """The request with its period lifted out as a typed slot, normalised: 'Why did profitability fall in Q1 2026?',
    '... in Q3 2026' and '... last quarter' share one pattern ('... fall {quarter}'); '... in 2025' does not."""
    t = question.lower()
    for p, slot in _PERIOD_PATTERNS:
        t = re.sub(p, f" {slot} ", t)
    t = re.sub(r"[^a-z0-9{}%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def signature(understanding: dict[str, Any], grain: str | None = None) -> str:
    """What makes two analyses the same workflow: the resolved request and the grain of its period input."""
    key = {k: understanding.get(k) for k in _UNDERSTANDING_KEYS if k not in ("matched", "audience")} | {"grain": grain}
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


def schema_fingerprint(schema: list[dict[str, Any]]) -> str:
    cols = sorted((c["name"], c["type"]) for c in schema)
    return hashlib.sha256(json.dumps(cols).encode()).hexdigest()[:16]


def _period_slots(period: dict[str, Any] | None, prior: dict | None, year_ago: dict | None) -> dict[str, dict]:
    return {k: v for k, v in (("current", period), ("prior", prior), ("year_ago", year_ago)) if v}


def _strip(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in ("label", "start", "end", "grain") if k in p}


def templatize(value: Any, slots: dict[str, dict]) -> Any:
    """Replace concrete period dicts in step arguments by {"$period": slot}."""
    if isinstance(value, dict):
        if {"start", "end"} <= value.keys():
            for name, p in slots.items():
                if _strip(value) == _strip(p):
                    return {"$period": name}
        return {k: templatize(v, slots) for k, v in value.items()}
    if isinstance(value, list):
        return [templatize(v, slots) for v in value]
    return value


def instantiate(value: Any, slots: dict[str, dict]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$period"}:
            if value["$period"] not in slots:
                raise ValueError(f"the workflow needs a {value['$period']} period that this run cannot derive")
            return dict(slots[value["$period"]])
        return {k: instantiate(v, slots) for k, v in value.items()}
    if isinstance(value, list):
        return [instantiate(v, slots) for v in value]
    return value


class WorkflowStale(ValueError):
    """The workflow's data dependencies changed since it was verified; it must not run as compiled."""


class WorkflowService:
    def __init__(self, stores, guard: Guard, data, semantic, registry) -> None:
        self.stores, self.guard, self.data, self.semantic, self.registry = stores, guard, data, semantic, registry

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- dependencies --------------------------------------------------------------------------

    def _current_deps(self, actor: Principal, ws: str, datasets: list[str], metrics: list[str]) -> dict[str, Any]:
        catalog = {d["name"]: d for d in self.data.catalog(actor, ws)}
        db = self._db(ws)
        return {
            "datasets": {n: (schema_fingerprint(catalog[n]["schema"]) if n in catalog else None) for n in sorted(datasets)},
            "metrics": {n: int(db.scalar("SELECT current_version FROM metrics WHERE name = ? AND status = 'approved'", [n]) or 0)
                        for n in sorted(metrics)},
        }

    def staleness(self, actor: Principal, ws: str, wf: dict[str, Any]) -> str | None:
        deps = wf["deps"]
        now = self._current_deps(actor, ws, list(deps["datasets"]), list(deps["metrics"]))
        for name, fp in deps["datasets"].items():
            if now["datasets"].get(name) is None:
                return f"dataset {name} is no longer available"
            if now["datasets"][name] != fp:
                return f"the schema of {name} changed since this workflow was verified"
        for name, v in deps["metrics"].items():
            if now["metrics"].get(name) != v:
                return (f"metric {name} is now v{now['metrics'].get(name)} (the workflow was verified on v{v})"
                        if now["metrics"].get(name) else f"metric {name} is no longer approved")
        return None

    def _mark_stale(self, ws: str, wf: dict[str, Any], reason: str) -> None:
        self._db(ws).execute("UPDATE workflows SET status = 'stale', invalid_reason = ? WHERE id = ? AND version = ?",
                             [reason, wf["id"], wf["version"]])

    # -- reading -------------------------------------------------------------------------------

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        for k in ("definition", "deps", "permissions"):
            if isinstance(out.get(k), str):
                out[k] = json.loads(out[k])
        return out

    def list(self, actor: Principal, ws: str, *, include_history: bool = False) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        where = "" if include_history else "WHERE status IN ('active', 'stale')"
        rows = self._db(ws).query(f"SELECT * FROM workflows {where} ORDER BY last_used_at DESC NULLS LAST, created_at DESC")
        return [self._row(r) for r in rows]

    def get(self, actor: Principal, ws: str, wf_id: str, version: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        db = self._db(ws)
        if version is None:
            rows = db.query("SELECT * FROM workflows WHERE id = ? ORDER BY version DESC LIMIT 1", [wf_id])
        else:
            rows = db.query("SELECT * FROM workflows WHERE id = ? AND version = ?", [wf_id, version])
        if not rows:
            raise KeyError(f"no workflow {wf_id}")
        wf = self._row(rows[0])
        wf["history"] = db.query("SELECT version, status, verified_runs, runs, created_at, invalid_reason, source_task "
                                 "FROM workflows WHERE id = ? ORDER BY version DESC", [wf_id])
        return wf

    def match(self, actor: Principal, ws: str, question: str) -> dict[str, Any] | None:
        """An active, current workflow whose request pattern is this question's, or None."""
        if not enabled():
            return None
        pat = pattern(question)
        rows = self._db(ws).query(
            "SELECT * FROM workflows WHERE status = 'active' AND list_contains(patterns, ?) "
            "ORDER BY verified_runs DESC LIMIT 1", [pat])
        if not rows:
            return None
        wf = self._row(rows[0])
        reason = self.staleness(actor, ws, wf)
        if reason:
            self._mark_stale(ws, wf, reason)
            return None
        return wf

    def for_run(self, actor: Principal, ws: str, ref: dict[str, Any]) -> dict[str, Any]:
        """The workflow a direct run names. Raises WorkflowStale or PermissionError instead of running stale or
        out-of-role work."""
        wf = self.get(actor, ws, ref["id"], ref.get("version"))
        if wf["status"] not in ("active",):
            raise WorkflowStale(f"workflow {wf['id']} v{wf['version']} is {wf['status']}"
                                + (f": {wf['invalid_reason']}" if wf.get("invalid_reason") else ""))
        reason = self.staleness(actor, ws, wf)
        if reason:
            self._mark_stale(ws, wf, reason)
            raise WorkflowStale(f"workflow {wf['id']} v{wf['version']} is stale: {reason}")
        for perm in wf["permissions"]:
            self.guard.require(actor, ws, perm, f"workflow:{wf['id']}")
        return wf

    # -- compiling -----------------------------------------------------------------------------

    def compile(self, actor: Principal, ws: str, task_id: str | None, understanding: dict[str, Any],
                context: dict[str, Any], plan: dict[str, Any], delivered: dict[str, Any],
                checks: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Compile a verified analysis. Returns {"id", "version", "created"} or None with nothing written."""
        if not enabled() or understanding.get("intent") not in COMPILABLE or plan.get("blockers"):
            return None
        if delivered.get("status") != "complete" or (delivered.get("confidence") or {}).get("level") != "verified":
            return None
        if not checks or any(not c["passed"] and c["severity"] == "error" for c in checks):
            return None
        steps = [s for s in plan["steps"]]
        caps = {s["capability"] for s in steps}
        if caps & _NOT_COMPILABLE_CAPS:
            return None
        for s in steps:
            cap = self.registry.get(s["capability"])
            if cap.mutating or cap.requires_approval or cap.origin != "core":
                return None
        period = context.get("period")
        slots: dict[str, dict] = {}
        if period:
            slots["current"] = period
            for s in steps:
                if s["key"] in ("prior", "year_ago") and "period" in s["args"]:
                    slots[s["key"]] = s["args"]["period"]
        template = [{k: s[k] for k in ("capability", "key", "after") if k in s} | {"args": templatize(s["args"], slots)}
                    for s in steps]
        und = {k: understanding.get(k) for k in _UNDERSTANDING_KEYS}
        sig = signature(understanding, period["grain"] if period else None)
        metrics = list(understanding.get("metrics") or [])
        datasets = sorted({m["spec"]["dataset"] for m in context.get("metrics", [])}
                          | ({understanding["dataset"]} if understanding.get("dataset") else set()))
        deps = self._current_deps(actor, ws, datasets, metrics)
        permissions = sorted({self.registry.get(s["capability"]).permission for s in steps} | {"analyst.ask", "task.create"})
        pat = pattern(understanding["question"])
        validation = {"intensity": plan.get("verification"), "required_checks": sorted(
            c["name"] for c in checks if c["severity"] == "error")}
        purpose = understanding["question"] if not period else re.sub(
            r"\s+", " ", understanding["question"].replace(period.get("label", "\0"), "{period}"))
        definition = {
            "purpose": purpose,
            "inputs": ({"period": {"type": "period", "grain": period["grain"],
                                   "default": f"the last complete {period['grain']} in the data"}} if period else {}),
            "understanding": und, "steps": template, "depth": plan.get("depth"),
            "validation": validation,
            "outputs": ["answer", "evidence", "charts", "tables", "confidence", "supporting"],
            "budget": {"max_steps": len(template) + 3},
            "success_criteria": plan.get("success_criteria", []),
        }
        db = self._db(ws)
        same = db.query("SELECT * FROM workflows WHERE signature = ? ORDER BY version DESC LIMIT 1", [sig])
        if same:
            cur = self._row(same[0])
            if (cur["status"] == "active" and json.dumps(cur["definition"]["steps"], sort_keys=True)
                    == json.dumps(template, sort_keys=True) and cur["deps"] == deps):
                patterns = sorted(set(cur["patterns"]) | {pat})
                db.execute("UPDATE workflows SET patterns = ?, verified_runs = verified_runs + 1, last_verified_at = now(), "
                           "last_verified_task = ? WHERE id = ? AND version = ?",
                           [patterns, task_id, cur["id"], cur["version"]])
                return {"id": cur["id"], "version": cur["version"], "created": False}
            wf_id, version = cur["id"], cur["version"] + 1
            patterns = sorted(set(cur["patterns"]) | {pat})
        else:
            wf_id, version, patterns = "wf_" + uuid.uuid4().hex[:10], 1, [pat]
        title = _title(understanding, period)

        def tx(c):
            c.execute("UPDATE workflows SET status = 'superseded' WHERE id = ? AND status IN ('active', 'stale')", [wf_id])
            c.execute(
                "INSERT INTO workflows (id, version, signature, title, intent, definition, deps, permissions, patterns, "
                "status, source_task, last_verified_task, verified_runs, runs, created_by, last_verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, 0, ?, now())",
                [wf_id, version, sig, title, understanding["intent"], json.dumps(definition, default=str), json.dumps(deps),
                 json.dumps(permissions), patterns, task_id, task_id, actor.user_id])

        db.write(tx)
        self.guard.audit(actor, ws, "workflow.compile", f"{wf_id}:v{version}", "ok")
        return {"id": wf_id, "version": version, "created": True}

    def plan_steps(self, wf: dict[str, Any], period: dict[str, Any] | None, prior: dict | None,
                   year_ago: dict | None) -> list[dict[str, Any]]:
        slots = _period_slots(period, prior, year_ago)
        return [dict(s, args=instantiate(s["args"], slots)) for s in wf["definition"]["steps"]]

    def record_run(self, ws: str, wf_id: str, version: int, *, verified: bool) -> None:
        self._db(ws).execute(
            "UPDATE workflows SET runs = runs + 1, last_used_at = now(), failed_verifications = failed_verifications + ? "
            "WHERE id = ? AND version = ?", [0 if verified else 1, wf_id, version])

    def run(self, actor: Principal, ws: str, wf_id: str, runtime, *, period: str | None = None) -> str:
        """Submit a compiled run for a period label ("Q4 2026", "Aug 2026") or, by default, the last complete one."""
        wf = self.for_run(actor, ws, {"id": wf_id})
        purpose = wf["definition"]["purpose"]
        grain = (wf["definition"]["inputs"].get("period") or {}).get("grain")
        if grain and not period:
            period = f"last {grain}" if grain in ("quarter", "month", "year") else None
        if period and not re.fullmatch(r"[A-Za-z0-9 '\-]{2,24}", period):
            raise ValueError("period must look like 'Q4 2026', 'Aug 2026', '2025' or 'last quarter'")
        if period and "{period}" in purpose:
            question = purpose.replace("{period}", period)
        elif period:
            question = f"{purpose} ({period})"
        else:
            question = purpose
        return runtime.submit(actor, ws, question, kind="workflow", workflow={"id": wf["id"], "version": wf["version"]})

    def retire(self, actor: Principal, ws: str, wf_id: str) -> None:
        self.guard.require(actor, ws, "skill.propose", wf_id)
        self._db(ws).execute("UPDATE workflows SET status = 'retired' WHERE id = ? AND status IN ('active', 'stale')", [wf_id])
        self.guard.audit(actor, ws, "workflow.retire", wf_id, "ok")


def _title(u: dict[str, Any], period: dict[str, Any] | None) -> str:
    metric = (u.get("metrics") or [u.get("dataset") or "data"])[0].replace("_", " ")
    kind = {"explain": "Why {m} changed", "value": "{m}", "breakdown": "{m} by {by}", "trend": "{m} trend",
            "forecast": "{m} forecast", "correlate": "{m} vs {o}", "profile": "Data quality of {m}"}[u["intent"]]
    t = kind.format(m=metric, by=(u.get("by") or "").replace("_", " "), o=(u.get("second_metric") or "").replace("_", " "))
    t = t[0].upper() + t[1:]
    if period:
        t += f", each {period['grain']}" if period.get("grain") in ("quarter", "month", "year") else ""
    if u.get("filters"):
        t += " (" + ", ".join(f"{f['column']} = {f['value']}" for f in u["filters"]) + ")"
    return t
