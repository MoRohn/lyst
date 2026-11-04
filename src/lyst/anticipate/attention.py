# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Attention management: a person's attention is a budget Lyst spends carefully.

Per person (``attention_prefs``): opt out of anticipation entirely, a minimum score, a daily cap on pushed items
(default 3), quiet hours in their own time zone, and kinds they do not want (``recurring_prep``, ``next_best_action``,
``early_warning``, ``health_foresight``). Applied in two places:

* **Push** (inbox notices): an item goes only to the people whose preferences allow it now. Quiet hours and the daily
  cap *hold* an item for its primary audience until they allow it; opting out, a muted kind, a score under the person's
  minimum or the same subject already pushed to them in the last 24 hours mean it is *not pushed* to them at all (it
  stays in the activity center).
* **Pull** (the activity center): opted-out people see nothing; muted kinds and items under their minimum score are left
  out.

Within one cycle, push candidates are taken highest score first, so a cap keeps the most valuable item.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_MAX_PER_DAY = 3
KINDS = ("recurring_prep", "next_best_action", "early_warning", "health_foresight")
HOLD = ("quiet_hours", "daily_cap")          # temporary: retried on a later cycle
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def prefs(db, user_id: str) -> dict[str, Any]:
    r = db.query("SELECT * FROM attention_prefs WHERE user_id = ?", [user_id])
    if not r:
        return {"user_id": user_id, "opted_out": False, "min_score": None, "max_per_day": DEFAULT_MAX_PER_DAY, "quiet_start": None,
                "quiet_end": None, "timezone": None, "muted_kinds": []}
    p = dict(r[0])
    p["muted_kinds"] = json.loads(p["muted_kinds"]) if isinstance(p["muted_kinds"], str) else p["muted_kinds"]
    if p["max_per_day"] is None:
        p["max_per_day"] = DEFAULT_MAX_PER_DAY
    p.pop("updated_at", None)
    return p


def save(db, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    p = prefs(db, user_id)
    for k in ("opted_out", "min_score", "max_per_day", "quiet_start", "quiet_end", "timezone", "muted_kinds"):
        if k in changes:
            p[k] = changes[k]
    if p["min_score"] is not None and not 0 <= float(p["min_score"]) <= 100:
        raise ValueError("min_score must be between 0 and 100")
    if p["max_per_day"] is not None and not 0 <= int(p["max_per_day"]) <= 50:
        raise ValueError("max_per_day must be between 0 and 50")
    for k in ("quiet_start", "quiet_end"):
        if p[k] in ("", None):
            p[k] = None
        elif not _HHMM.match(str(p[k])):
            raise ValueError(f"{k} must be HH:MM")
    if (p["quiet_start"] is None) != (p["quiet_end"] is None):
        raise ValueError("set both quiet_start and quiet_end, or neither")
    if p["timezone"]:
        try:
            ZoneInfo(p["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {p['timezone']}") from exc
    bad = [k for k in p["muted_kinds"] or [] if k not in KINDS]
    if bad:
        raise ValueError(f"unknown kinds {', '.join(bad)}; choose from {', '.join(KINDS)}")
    db.execute("INSERT OR REPLACE INTO attention_prefs (user_id, opted_out, min_score, max_per_day, quiet_start, quiet_end, timezone, "
               "muted_kinds, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
               [user_id, bool(p["opted_out"]), p["min_score"], p["max_per_day"], p["quiet_start"], p["quiet_end"], p["timezone"],
                json.dumps(p["muted_kinds"] or [])])
    return prefs(db, user_id)


def _quiet(p: dict[str, Any], now: datetime) -> bool:
    if not p["quiet_start"]:
        return False
    tz = ZoneInfo(p["timezone"]) if p["timezone"] else UTC
    local = now.astimezone(tz).time()
    start, end = (time.fromisoformat(p[k]) for k in ("quiet_start", "quiet_end"))
    return start <= local < end if start < end else (local >= start or local < end)


def shown(p: dict[str, Any], a: dict[str, Any]) -> bool:
    """Whether the activity center shows this item to this person."""
    if p["opted_out"] or a["kind"] in (p["muted_kinds"] or []):
        return False
    return p["min_score"] is None or a["score"] >= float(p["min_score"])


def _pushed_today(db, user_id: str, now: datetime) -> int:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(db.scalar("SELECT count(*) FROM notifications n JOIN findings f ON f.id = n.finding_id WHERE n.user_id = ? "
                         "AND n.channel = 'inbox' AND f.monitor_id LIKE 'anticipate:%' AND n.created_at >= ?", [user_id, start]) or 0)


def _same_subject_recently(db, user_id: str, a: dict[str, Any], now: datetime) -> bool:
    return bool(db.scalar("SELECT count(*) FROM notifications n JOIN findings f ON f.id = n.finding_id WHERE n.user_id = ? "
                          "AND f.monitor_id LIKE 'anticipate:%' AND f.metric = ? AND n.created_at >= ?",
                          [user_id, f"{a['subject_kind']}:{a['subject_id']}", now - timedelta(hours=24)]))


def push_decision(db, user_id: str, a: dict[str, Any], now: datetime) -> str | None:
    """None when this person may be told now, else why not (one of HOLD, or a permanent reason)."""
    p = prefs(db, user_id)
    if p["opted_out"]:
        return "opted_out"
    if a["kind"] in (p["muted_kinds"] or []):
        return "muted_kind"
    if p["min_score"] is not None and a["score"] < float(p["min_score"]):
        return "below_min_score"
    if _same_subject_recently(db, user_id, a, now):
        return "duplicate_subject"
    if _quiet(p, now):
        return "quiet_hours"
    if _pushed_today(db, user_id, now) >= int(p["max_per_day"]):
        return "daily_cap"
    return None
