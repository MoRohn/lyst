# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Measured model performance: a scorecard per model and workload from Lyst's own call records, and routing that
uses it.

Every upstream call is a row in ``llm_calls`` (outcome, latency, tokens, cost, workload). The scorecard turns the last
days of those rows into success rate, latency and cost per model and workload. Routing uses it conservatively: when a
workspace routes automatically (a tier, not a pinned model), a model whose recent calls for this workload mostly
failed (``LYST_ROUTE_MIN_CALLS`` calls or more in the last ``LYST_ROUTE_WINDOW_HOURS``, success below
``LYST_ROUTE_MIN_SUCCESS``) moves behind the healthy models of the same tier. It is never removed (it stays a
fallback), never promoted above its tier, and a pinned model is never reordered. Quality on analytical work is
measured separately by the evaluation suite (``lyst eval``), whose runs record the model used.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

FAILED = ("error", "upstream_error", "timeout", "network", "empty", "truncated", "rejected")


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def scorecard(db, *, days: int = 7, workspace_id: str | None = None) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    where = " AND workspace_id = ?" if workspace_id else ""
    rows = db.query(
        "SELECT model, coalesce(workload, 'chat') AS workload, count(*) AS calls, "
        "count(*) FILTER (WHERE outcome = 'ok') AS ok, "
        f"count(*) FILTER (WHERE outcome IN ({', '.join('?' * len(FAILED))})) AS failed, "
        "count(*) FILTER (WHERE outcome = 'throttled') AS throttled, "
        "quantile_cont(latency_ms, 0.5) FILTER (WHERE outcome = 'ok') AS p50_ms, "
        "quantile_cont(latency_ms, 0.95) FILTER (WHERE outcome = 'ok') AS p95_ms, "
        "avg(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)) FILTER (WHERE outcome = 'ok') AS tokens, "
        "avg(cost_usd) FILTER (WHERE outcome = 'ok') AS cost "
        f"FROM llm_calls WHERE occurred_at >= ? AND model IS NOT NULL{where} GROUP BY 1, 2 ORDER BY 1, 2",
        [*FAILED, since, *([workspace_id] if workspace_id else [])])
    out = []
    for r in rows:
        decided = r["ok"] + r["failed"]
        out.append({"model": r["model"], "workload": r["workload"], "calls": r["calls"],
                    "success_rate": round(r["ok"] / decided, 4) if decided else None, "throttled": r["throttled"],
                    "p50_ms": r["p50_ms"], "p95_ms": r["p95_ms"],
                    "avg_tokens": round(r["tokens"], 1) if r["tokens"] is not None else None,
                    "avg_cost_usd": round(r["cost"], 8) if r["cost"] is not None else None})
    return out


def unhealthy(db, workload: str, models: list[str]) -> dict[str, dict[str, Any]]:
    """Models among ``models`` whose recent calls for ``workload`` mostly failed, with the evidence."""
    if not models:
        return {}
    window = datetime.now(UTC) - timedelta(hours=_env("LYST_ROUTE_WINDOW_HOURS", 24))
    min_calls, min_success = int(_env("LYST_ROUTE_MIN_CALLS", 10)), _env("LYST_ROUTE_MIN_SUCCESS", 0.7)
    rows = db.query(
        f"SELECT model, count(*) FILTER (WHERE outcome = 'ok') AS ok, "
        f"count(*) FILTER (WHERE outcome IN ({', '.join('?' * len(FAILED))})) AS failed FROM llm_calls "
        f"WHERE occurred_at >= ? AND coalesce(workload, 'chat') = ? AND model IN ({', '.join('?' * len(models))}) "
        "GROUP BY model", [*FAILED, window, workload, *models])
    bad = {}
    for r in rows:
        decided = r["ok"] + r["failed"]
        if decided >= min_calls and r["ok"] / decided < min_success:
            bad[r["model"]] = {"calls": decided, "success_rate": round(r["ok"] / decided, 3)}
    return bad


def measured_order(db, workload: str, models: list[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """``models`` with the recently failing ones moved last (stable otherwise)."""
    bad = unhealthy(db, workload, models)
    return sorted(models, key=lambda m: m in bad), bad


__all__ = ["measured_order", "scorecard", "unhealthy"]
