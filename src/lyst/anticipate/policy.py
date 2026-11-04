# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Autonomy levels and budgets for anticipative work.

The level says how far Lyst may go *before being asked*. It never widens what a person may do: every operation still
runs as a real member through the guard, and anything consequential (sending, publishing, changing data, definitions or
deliverables, deploying a capability) goes through the existing approvals path at every level.

====  ===========  =====================================================================================================
0     off          nothing runs in the background; open anticipations are withdrawn
1     suggest      metadata only: Lyst notices likely needs and lists them, with no analytical execution
2     prepare      bounded read-only checks: freshness, schemas, permissions, definitions, dependencies, cost estimates
3     investigate  read-only investigations run as background tasks (maintenance priority) to verify a finding first
4     automate     an approved recurring or event-driven run may be started early by its preparation
====  ===========  =====================================================================================================
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

LEVELS = {0: "off", 1: "suggest", 2: "prepare", 3: "investigate", 4: "automate"}
NAMES = {v: k for k, v in LEVELS.items()}
# the lowest level at which each operation class may run
OPERATIONS = {"observe": 1, "suggest": 1, "prepare": 2, "investigate": 3, "automate": 4}
DEFAULT_LEVEL = 1  # a workspace that never chose sees suggestions, nothing executes


@dataclass
class Settings:
    level: int = DEFAULT_LEVEL
    daily_prepare_limit: int = 50          # preparations per workspace per day
    daily_investigation_limit: int = 5     # background investigations per workspace per day
    daily_compute_ms: int = 120_000        # preparation and investigation wall time per day
    daily_surface_limit: int = 5           # pushed notices per workspace per day (the activity center is not limited)
    updated_by: str | None = None
    updated_at: Any = None

    @property
    def name(self) -> str:
        return LEVELS[self.level]

    def allows(self, operation: str) -> bool:
        return self.level >= OPERATIONS[operation]

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["level_name"] = self.name
        d["allows"] = {op: self.allows(op) for op in OPERATIONS}
        return d


def parse_level(value: int | str) -> int:
    if isinstance(value, str) and not value.isdigit():
        if value not in NAMES:
            raise ValueError(f"level must be 0-4 or one of {', '.join(NAMES)}")
        return NAMES[value]
    level = int(value)
    if level not in LEVELS:
        raise ValueError("level must be between 0 (off) and 4 (automate)")
    return level


def load(db) -> Settings:
    rows = db.query("SELECT * FROM anticipation_settings WHERE id = 'workspace'")
    if not rows:
        return Settings()
    r = rows[0]
    return Settings(level=int(r["level"]), daily_prepare_limit=int(r["daily_prepare_limit"]),
                    daily_investigation_limit=int(r["daily_investigation_limit"]), daily_compute_ms=int(r["daily_compute_ms"]),
                    daily_surface_limit=int(r["daily_surface_limit"]), updated_by=r["updated_by"], updated_at=r["updated_at"])


LIMITS = {"daily_prepare_limit": (0, 10_000), "daily_investigation_limit": (0, 500), "daily_compute_ms": (0, 86_400_000),
          "daily_surface_limit": (0, 100)}


def save(db, actor_id: str, current: Settings, changes: dict[str, Any]) -> Settings:
    s = Settings(**{k: v for k, v in asdict(current).items()})
    if changes.get("level") is not None:
        s.level = parse_level(changes["level"])
    for key, (lo, hi) in LIMITS.items():
        if changes.get(key) is not None:
            v = int(changes[key])
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            setattr(s, key, v)
    s.updated_by = actor_id
    db.execute("INSERT OR REPLACE INTO anticipation_settings (id, level, daily_prepare_limit, daily_investigation_limit, "
               "daily_compute_ms, daily_surface_limit, updated_by, updated_at) VALUES ('workspace', ?, ?, ?, ?, ?, ?, now())",
               [s.level, s.daily_prepare_limit, s.daily_investigation_limit, s.daily_compute_ms, s.daily_surface_limit, actor_id])
    return load(db)


# -- budgets -------------------------------------------------------------------------------------------------------------

def today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def usage(db, day: date | None = None) -> dict[str, dict[str, int]]:
    rows = db.query("SELECT operation, count, compute_ms, refused FROM anticipation_usage WHERE day = ?", [day or today()])
    return {r["operation"]: {"count": int(r["count"]), "compute_ms": int(r["compute_ms"]), "refused": int(r["refused"])} for r in rows}


def _limit_for(s: Settings, operation: str) -> int | None:
    return {"prepare": s.daily_prepare_limit, "investigate": s.daily_investigation_limit,
            "surface": s.daily_surface_limit}.get(operation)


def admit(db, s: Settings, operation: str, now: datetime | None = None) -> str | None:
    """None when the operation fits today's budget, else the reason it does not (and the refusal is counted)."""
    used = usage(db, today(now))
    limit = _limit_for(s, operation)
    count = used.get(operation, {}).get("count", 0)
    compute = sum(u["compute_ms"] for u in used.values())
    reason = None
    if limit is not None and count >= limit:
        reason = f"today's {operation} budget ({limit}) is used"
    elif operation in ("prepare", "investigate") and compute >= s.daily_compute_ms:
        reason = f"today's background compute budget ({s.daily_compute_ms // 1000}s) is used"
    if reason:
        _bump(db, today(now), operation, refused=1)
    return reason


def charge(db, operation: str, compute_ms: int = 0, now: datetime | None = None) -> None:
    _bump(db, today(now), operation, count=1, compute_ms=compute_ms)


def _bump(db, day: date, operation: str, *, count: int = 0, compute_ms: int = 0, refused: int = 0) -> None:
    def tx(c):
        c.execute("INSERT INTO anticipation_usage (day, operation, count, compute_ms, refused) VALUES (?, ?, 0, 0, 0) "
                  "ON CONFLICT DO NOTHING", [day, operation])
        c.execute("UPDATE anticipation_usage SET count = count + ?, compute_ms = compute_ms + ?, refused = refused + ? "
                  "WHERE day = ? AND operation = ?", [count, compute_ms, refused, day, operation])

    db.write(tx)
