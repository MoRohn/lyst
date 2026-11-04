# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Learning from what people did with anticipations, and measuring whether anticipation is any good.

**Outcomes.** Accepting an offered action, marking an item useful, or completing it counts for its *family* (kind plus
the action it offered first: "next_best_action/watch" learns separately from "next_best_action/run_now"). Dismissing it,
or marking it not useful or incorrect, counts against. Expired, withdrawn and resolved items count neither way: nobody
judged them.

**Ranking.** A family's weight is ``2 * (positive + 4) / (positive + negative + 8)``, clamped to 0.5-1.5: a Beta(4, 4)
prior, so one acceptance or dismissal moves the weight by about a tenth and it takes a pattern to move it far. Only
outcomes from the last ``LYST_ANTICIPATION_LEARN_DAYS`` (default 180) count.

**Repeated irrelevance.** Two dismissals (or not-useful / incorrect marks) of the same family about the same subject within
30 days stop new items of that family about that subject for 30 days.

**Retention.** Closed items and their events older than ``LYST_RETENTION_DAYS_ANTICIPATION`` (default 180) are deleted;
usage rows older than 400 days too.

**Quality.** ``quality()`` reports relevance, timeliness, accuracy, efficiency, attention burden and governance from
recorded objects and events only. No model-generated confidence is used anywhere.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

POSITIVE = ("accepted", "feedback:useful", "completed")
NEGATIVE = ("dismissed", "feedback:not_useful", "feedback:incorrect")
PRIOR = 4.0


def family(kind: str, actions: list[dict[str, Any]] | str | None) -> str:
    acts = json.loads(actions) if isinstance(actions, str) else (actions or [])
    return f"{kind}/{acts[0]['id']}" if acts else kind


def _since(days: int | None = None) -> datetime:
    return datetime.now(UTC) - timedelta(days=days or int(os.environ.get("LYST_ANTICIPATION_LEARN_DAYS", "180")))


def judgements(db, since: datetime) -> list[dict[str, Any]]:
    """One row per judging event: the item's family and subject, and whether it was for or against."""
    rows = db.query("SELECT e.event, e.occurred_at, a.kind, a.actions, a.subject_id FROM anticipation_events e JOIN anticipations a "
                    "ON a.id = e.anticipation_id WHERE e.occurred_at >= ? AND e.event IN "
                    f"({', '.join('?' for _ in POSITIVE + NEGATIVE)})", [since, *POSITIVE, *NEGATIVE])
    return [{"family": family(r["kind"], r["actions"]), "subject": r["subject_id"], "positive": r["event"] in POSITIVE,
             "at": r["occurred_at"]} for r in rows]


def weights(db) -> dict[str, float]:
    counts: dict[str, list[int]] = {}
    for j in judgements(db, _since()):
        c = counts.setdefault(j["family"], [0, 0])
        c[0 if j["positive"] else 1] += 1
    return {f: round(min(max(2 * (p + PRIOR) / (p + n + 2 * PRIOR), 0.5), 1.5), 3) for f, (p, n) in counts.items()}


def irrelevant(db, now: datetime) -> set[tuple[str, str]]:
    """(family, subject) pairs judged irrelevant twice in the last 30 days."""
    seen: dict[tuple[str, str], int] = {}
    for j in judgements(db, now - timedelta(days=30)):
        if not j["positive"]:
            seen[(j["family"], j["subject"])] = seen.get((j["family"], j["subject"]), 0) + 1
    return {k for k, n in seen.items() if n >= 2}


def prune(db, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=int(os.environ.get("LYST_RETENTION_DAYS_ANTICIPATION", "180")))
    from .model import TERMINAL  # noqa: PLC0415

    ids = [r["id"] for r in db.query(f"SELECT id FROM anticipations WHERE status IN {TERMINAL} AND updated_at < ?", [cutoff])]

    def tx(c):
        for i in ids:
            c.execute("DELETE FROM anticipation_events WHERE anticipation_id = ?", [i])
            c.execute("DELETE FROM anticipations WHERE id = ?", [i])
        c.execute("DELETE FROM anticipation_usage WHERE day < ?", [(now - timedelta(days=400)).date()])

    db.write(tx)
    return len(ids)


# -- quality ---------------------------------------------------------------------------------------------------------------

def _aware(ts):
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=UTC)


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def quality(stores, ws: str, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    db = stores.workspace(ws)
    items = db.query("SELECT * FROM anticipations WHERE created_at >= ?", [since])
    events = db.query("SELECT e.anticipation_id, e.event, e.detail, e.occurred_at, a.kind FROM anticipation_events e "
                      "JOIN anticipations a ON a.id = e.anticipation_id WHERE e.occurred_at >= ?", [since])
    js = judgements(db, since)
    pos = sum(j["positive"] for j in js)
    neg = len(js) - pos
    by_kind: dict[str, dict[str, int]] = {}
    for j in js:
        k = by_kind.setdefault(j["family"], {"positive": 0, "negative": 0})
        k["positive" if j["positive"] else "negative"] += 1
    # timeliness: prepared before it was needed, and by how much
    leads = [(_aware(a["needed_at"]) - _aware(a["prepared_at"])).total_seconds() / 3600 for a in items
             if a["needed_at"] and a["prepared_at"]]
    # accuracy: early warnings that an investigation confirmed versus closed as noise, and monitor alerts nobody warned about
    ew = [a for a in items if a["kind"] == "early_warning"]
    investigated = [a for a in ew if a["task_id"]]
    confirmed = [a for a in investigated if a["verdict"] == "verified"]
    dropped = [a for a in investigated if a["status"] == "suppressed"]
    warned = {a["subject_id"] for a in ew if a["status"] not in ("suppressed",)}
    alerts = db.query("SELECT DISTINCT metric FROM findings WHERE kind IN ('metric_shift', 'forecast_deviation') AND created_at >= ? "
                      "AND monitor_id NOT LIKE 'anticipate:%'", [since])
    missed = sorted(r["metric"] for r in alerts if r["metric"] not in warned)
    incorrect = sum(1 for e in events if e["event"] == "feedback:incorrect")
    # efficiency
    res = [json.loads(a["resources"]) if isinstance(a["resources"], str) else (a["resources"] or {}) for a in items]
    blocked_early = sum(1 for a in items if a["kind"] == "recurring_prep" and a["verdict"] == "blocked")
    # attention
    pushes = [e for e in events if e["event"] == "surfaced"]
    per_day = statistics.mean([len([p for p in pushes if _aware(p["occurred_at"]).date() == d])
                               for d in {_aware(p["occurred_at"]).date() for p in pushes}]) if pushes else 0.0
    # governance: every accepted item audited; every background task ran as someone the item was for
    accepted = [a for a in items if a["status"] == "accepted"]
    audited = stores.control.query("SELECT target FROM audit_log WHERE workspace_id = ? AND action = 'anticipation.accept' AND occurred_at >= ?",
                                   [ws, since])
    audited_ids = {r["target"] for r in audited}
    off_audience = 0
    for a in items:
        if a["task_id"]:
            by = stores.control.scalar("SELECT created_by FROM tasks WHERE id = ?", [a["task_id"]])
            aud = json.loads(a["audience"]) if isinstance(a["audience"], str) else a["audience"]
            if by and by not in aud and a["decided_by"] != by:
                off_audience += 1
    denied = stores.control.scalar("SELECT count(*) FROM audit_log WHERE workspace_id = ? AND action LIKE 'anticipation.%' "
                                   "AND outcome = 'denied'", [ws]) or 0
    return {
        "window_days": days, "items": len(items),
        "relevance": {"judged": len(js), "positive": pos, "negative": neg, "useful_rate": _rate(pos, len(js)), "by_family": by_kind,
                      "repeated_irrelevant": len(irrelevant(db, now))},
        "timeliness": {"with_need_time": len(leads), "prepared_before_needed": _rate(sum(1 for x in leads if x > 0), len(leads)),
                       "median_lead_hours": round(statistics.median(leads), 2) if leads else None},
        "accuracy": {"early_warnings": len(ew), "investigated": len(investigated), "confirmed": len(confirmed),
                     "closed_as_noise": len(dropped), "false_positive_rate": _rate(len(dropped), len(investigated)),
                     "monitor_alerts_without_warning": missed, "marked_incorrect": incorrect},
        "efficiency": {"compute_ms": sum(int(r.get("compute_ms", 0)) for r in res), "queries": sum(int(r.get("queries", 0)) for r in res),
                       "model_calls": sum(int(r.get("model_calls", 0)) for r in res), "background_tasks": sum(int(r.get("tasks", 0)) for r in res),
                       "refreshes": sum(int(r.get("refreshes", 0)) for r in res), "runs_blocked_before_they_failed": blocked_early},
        "attention": {"pushed": len(pushes), "pushes_per_active_day": round(per_day, 2),
                      "held": sum(1 for e in events if e["event"] == "held"), "not_pushed": sum(1 for e in events if e["event"] == "not_pushed"),
                      "suppressed": sum(1 for a in items if a["status"] == "suppressed")},
        "governance": {"accepted": len(accepted), "accepted_without_audit": len([a for a in accepted if a["id"] not in audited_ids]),
                       "tasks_run_as_someone_else": off_audience, "denied_attempts": int(denied)},
        "basis": "recorded anticipation objects, events, audit log and monitor findings; no model-generated confidence",
    }

