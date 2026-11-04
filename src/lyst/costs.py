# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What Lyst costs to run, what it delivered, and where it could be cheaper, from records it already keeps.

Cost comes from the model-call log (provider-reported or catalog-priced cost, tokens, retries and failures), the
step log (compute time), the query log (query time and rows processed), connector events (external API use) and
the files on disk (storage). It can be grouped by user, task, schedule, workflow, model or purpose.

Value is counted, not claimed: verified answers, time from request to a verified answer, scheduled reports that
ran, answers served by a compiled workflow instead of a fresh plan, monitor findings raised, and suggestions
accepted. Effort avoided is computed only when an administrator supplies the minutes a manual answer takes, and the
assumption is returned with the number. No return-on-investment figure is produced.

Recommendations are grounded in the same numbers and cite them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .security import Guard, Principal

GROUPS = {
    "user": "t.created_by",
    "task": "c.task_id",
    "schedule": "t.schedule_id",
    "model": "c.model",
    "purpose": "coalesce(c.workload, c.purpose)",
    "workflow": "t.request->>'$.workflow.id'",
}


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())).strip()


class CostService:
    def __init__(self, stores, guard: Guard, settings) -> None:
        self.stores = stores
        self.guard = guard
        self.settings = settings

    def _since(self, days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    # -- cost -----------------------------------------------------------------------------------

    def costs(self, actor: Principal, ws: str, *, days: int = 30, by: str = "user") -> dict[str, Any]:
        self.guard.require(actor, ws, "audit.read")
        if by not in GROUPS:
            raise ValueError(f"by is one of {', '.join(GROUPS)}")
        since = self._since(days)
        ctl = self.stores.control
        model = ctl.query(
            "SELECT count(*) AS calls, count(*) FILTER (WHERE c.outcome <> 'ok') AS failed_calls, "
            "count(*) FILTER (WHERE coalesce(c.attempt, 1) > 1) AS retried_calls, "
            "coalesce(sum(c.cost_usd), 0) AS cost_usd, coalesce(sum(c.cost_usd) FILTER (WHERE c.outcome <> 'ok' OR coalesce(c.attempt, 1) > 1), 0) AS retry_cost_usd, "
            "coalesce(sum(c.input_tokens), 0) AS input_tokens, coalesce(sum(c.output_tokens), 0) AS output_tokens, "
            "count(*) FILTER (WHERE c.external) AS external_calls, coalesce(sum(c.latency_ms), 0) AS model_ms "
            "FROM llm_calls c WHERE c.workspace_id = ? AND c.occurred_at >= ?", [ws, since])[0]
        steps = ctl.query(
            "SELECT count(DISTINCT s.task_id) AS tasks, count(*) AS steps, "
            "coalesce(sum(date_diff('millisecond', s.started_at, s.finished_at)), 0) AS compute_ms "
            "FROM task_steps s JOIN tasks t ON t.id = s.task_id WHERE t.workspace_id = ? AND t.created_at >= ?", [ws, since])[0]
        db = self.stores.workspace(ws)
        queries = db.query("SELECT count(*) AS queries, coalesce(sum(ms), 0) AS query_ms, coalesce(sum(rows), 0) AS rows_returned, "
                           "count(*) FILTER (WHERE outcome <> 'ok') AS failed_queries FROM query_history WHERE occurred_at >= ?", [since])[0]
        external = ctl.query("SELECT count(*) AS operations, coalesce(sum(attempts), 0) AS attempts FROM connection_events "
                             "WHERE workspace_id = ? AND occurred_at >= ?", [ws, since])[0]
        expr = GROUPS[by]
        groups = ctl.query(
            f"SELECT {expr} AS key, count(*) AS calls, round(coalesce(sum(c.cost_usd), 0), 6) AS cost_usd, "
            "coalesce(sum(c.input_tokens), 0) + coalesce(sum(c.output_tokens), 0) AS tokens, "
            "count(*) FILTER (WHERE c.outcome <> 'ok' OR coalesce(c.attempt, 1) > 1) AS retries "
            "FROM llm_calls c LEFT JOIN tasks t ON t.id = c.task_id WHERE c.workspace_id = ? AND c.occurred_at >= ? "
            "GROUP BY ALL ORDER BY cost_usd DESC, calls DESC LIMIT 50", [ws, since])
        if by == "user":
            names = {r["id"]: r["name"] for r in ctl.query("SELECT id, name FROM users")}
            for g in groups:
                g["label"] = names.get(g["key"], "background work" if g["key"] is None else g["key"])
        storage = self._storage(ws)
        return {"days": days, "by": by,
                "totals": {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in model.items()}, **steps, **queries,
                           "external_operations": external["operations"], "external_attempts": external["attempts"], **storage},
                "groups": groups}

    def _storage(self, ws: str) -> dict[str, int]:
        path = self.settings.workspace_db(ws)
        db_bytes = path.stat().st_size if path.exists() else 0
        art = self.stores.control.scalar("SELECT coalesce(sum(bytes), 0) FROM artifacts WHERE workspace_id = ?", [ws]) or 0
        return {"database_bytes": int(db_bytes), "artifact_bytes": int(art)}

    # -- value ----------------------------------------------------------------------------------

    def value(self, actor: Principal, ws: str, *, days: int = 30, manual_minutes_per_answer: float | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "audit.read")
        since = self._since(days)
        ctl = self.stores.control
        t = ctl.query(
            "SELECT count(*) FILTER (WHERE status = 'done') AS finished, "
            "count(*) FILTER (WHERE status = 'done' AND result->>'$.status' = 'complete') AS verified, "
            "count(*) FILTER (WHERE status = 'failed') AS failed, "
            "count(*) FILTER (WHERE status = 'done' AND schedule_id IS NOT NULL) AS scheduled_runs, "
            "quantile_cont(date_diff('second', created_at, updated_at), 0.5) FILTER (WHERE status = 'done' AND result->>'$.status' = 'complete') AS median_seconds_to_verified "
            "FROM tasks WHERE workspace_id = ? AND created_at >= ?", [ws, since])[0]
        db = self.stores.workspace(ws)
        compiled = db.scalar("SELECT count(*) FROM analyses WHERE created_at >= ? AND evidence->>'$.workflow.used' IS NOT NULL "
                             "AND evidence->>'$.workflow.used' <> 'null'", [since]) or 0
        findings = db.scalar("SELECT count(*) FROM findings WHERE created_at >= ?", [since]) if self._has(db, "findings", "created_at") else None
        accepted = db.scalar("SELECT count(*) FROM onboarding_proposals WHERE status = 'accepted' AND decided_at >= ?", [since]) or 0
        assets = db.scalar("SELECT count(*) FROM assets WHERE status = 'active'") or 0
        out = {"days": days, "verified_answers": t["verified"], "finished_tasks": t["finished"], "failed_tasks": t["failed"],
               "verification_rate": round(t["verified"] / t["finished"], 3) if t["finished"] else None,
               "median_seconds_to_verified": t["median_seconds_to_verified"], "scheduled_reports_run": t["scheduled_runs"],
               "answers_from_compiled_workflows": compiled, "monitor_findings": findings, "suggestions_accepted": accepted,
               "active_reports_and_dashboards": assets}
        if manual_minutes_per_answer is not None:
            if not 0 < manual_minutes_per_answer <= 24 * 60:
                raise ValueError("manual_minutes_per_answer is between 0 and 1440")
            out["effort_avoided_hours"] = {"estimate": round(t["verified"] * manual_minutes_per_answer / 60, 1),
                                           "assumption": f"{manual_minutes_per_answer:g} minutes per manual answer, supplied by you",
                                           "basis": f"{t['verified']} verified answers"}
        return out

    @staticmethod
    def _has(db, table: str, column: str) -> bool:
        return bool(db.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = ? AND column_name = ?", [table, column]))

    # -- recommendations ------------------------------------------------------------------------

    def recommendations(self, actor: Principal, ws: str, *, days: int = 30) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "audit.read")
        since = self._since(days)
        ctl = self.stores.control
        out: list[dict[str, Any]] = []
        repeated: dict[str, list[dict[str, Any]]] = {}
        for t in ctl.query("SELECT id, title, schedule_id, request FROM tasks WHERE workspace_id = ? AND created_at >= ? AND status = 'done'",
                           [ws, since]):
            if t["schedule_id"] is None:
                repeated.setdefault(_norm(t["title"]), []).append(t)
        for key, ts in sorted(repeated.items(), key=lambda kv: -len(kv[1])):
            if len(ts) >= 3 and key:
                cost = ctl.scalar(f"SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE task_id IN ({','.join('?' * len(ts))})", [t["id"] for t in ts])
                out.append({"kind": "schedule_repeated_question", "title": f"Schedule \"{ts[0]['title'][:80]}\"",
                            "why": f"asked {len(ts)} times in {days} days" + (f", ${cost:.4f} in model cost" if cost else ""),
                            "action": "Make it a scheduled report so it runs once on a cadence and is shared, instead of re-asked.",
                            "evidence": {"tasks": [t["id"] for t in ts[:10]]}})
        for m in ctl.query("SELECT model, count(*) AS calls, count(*) FILTER (WHERE outcome <> 'ok') AS failed, "
                           "coalesce(sum(cost_usd) FILTER (WHERE outcome <> 'ok' OR coalesce(attempt, 1) > 1), 0) AS wasted "
                           "FROM llm_calls WHERE workspace_id = ? AND occurred_at >= ? GROUP BY model HAVING count(*) >= 5", [ws, since]):
            if m["failed"] / m["calls"] >= 0.2:
                out.append({"kind": "reroute_model", "title": f"Route away from {m['model']}",
                            "why": f"{m['failed']} of {m['calls']} calls failed" + (f", ${m['wasted']:.4f} spent on retries" if m["wasted"] else ""),
                            "action": "Pin a different model or lower the tier for this workspace in Settings.", "evidence": {"model": m["model"]}})
        db = self.stores.workspace(ws)
        for q in db.query("SELECT datasets, count(*) AS n, quantile_cont(ms, 0.95) AS p95 FROM query_history "
                          "WHERE occurred_at >= ? AND outcome = 'ok' AND datasets IS NOT NULL GROUP BY datasets HAVING count(*) >= 5", [since]):
            if (q["p95"] or 0) >= 2000:
                out.append({"kind": "slow_queries", "title": f"Speed up queries on {q['datasets']}",
                            "why": f"95th percentile {q['p95'] / 1000:.1f} s over {q['n']} queries",
                            "action": "Pre-aggregate the dataset with a verified transformation, or narrow the metric's dimensions.",
                            "evidence": {"datasets": q["datasets"]}})
        planned = db.query("SELECT question, count(*) AS n FROM analyses WHERE created_at >= ? AND (evidence->>'$.workflow.used' IS NULL "
                           "OR evidence->>'$.workflow.used' = 'null') GROUP BY question HAVING count(*) >= 3 ORDER BY n DESC LIMIT 3", [since])
        for p in planned:
            if not any(r["kind"] == "schedule_repeated_question" and _norm(p["question"])[:60] in _norm(r["title"]) for r in out):
                out.append({"kind": "compile_workflow", "title": f"Reuse the verified method for \"{p['question'][:80]}\"",
                            "why": f"planned from scratch {p['n']} times in {days} days",
                            "action": "Lyst compiles a workflow after a verified run; check Settings › Advanced that workflows are on "
                                      "and that the definitions it uses have not changed between runs.",
                            "evidence": {"question": p["question"]}})
        return out
