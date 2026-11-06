# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Is Lyst getting better? One report, counted from stored records only (docs/LEARNING.md).

- **Answers**: the verdicts people gave, and how many analyses stand confirmed or disputed.
- **Workflows**: how much work reuses a verified workflow, and which are held back and why (a person disputed an
  answer, runs kept failing verification, or the data or definitions changed).
- **Harness rules**: for each rule in force, whether the failure it targets stopped (``harness.rule_effect``).
- **Memory**: what context can recall, by trust, and what is held back.

No model judgment and no confidence score feeds any number here.
"""

from __future__ import annotations

from typing import Any

from .feedback import DISPUTED
from .harness import rule_effect


def _held_back_because(reason: str | None) -> str:
    r = reason or ""
    if r.startswith(DISPUTED):
        return "disputed by a person"
    if "failed verification" in r:
        return "kept failing verification"
    return "data or definitions changed"


class LearningService:
    def __init__(self, stores, guard, feedback) -> None:
        self.stores, self.guard, self.feedback = stores, guard, feedback

    def report(self, actor, ws: str, days: int = 90) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        db = self.stores.workspace(ws)
        answers = self.feedback.summary(actor, ws, days=days)

        wfs = db.query("SELECT id, version, status, invalid_reason, runs, verified_runs, failed_verifications "
                       "FROM workflows")
        current = [w for w in wfs if w["status"] in ("active", "stale")]
        held: dict[str, int] = {}
        for w in current:
            if w["status"] == "stale":
                k = _held_back_because(w["invalid_reason"])
                held[k] = held.get(k, 0) + 1
        compiled_runs = sum(int(w["runs"] or 0) for w in wfs)
        failed = sum(int(w["failed_verifications"] or 0) for w in wfs)
        analyses = int(db.scalar("SELECT count(*) FROM analyses") or 0)
        workflows = {
            "active": sum(1 for w in current if w["status"] == "active"),
            "held_back": held,
            "compiled_runs": compiled_runs,
            "compiled_runs_verified_rate": round((compiled_runs - failed) / compiled_runs, 3) if compiled_runs else None,
            "analyses": analyses,
            "reuse_share": round(compiled_runs / analyses, 3) if analyses else None,
        }

        ctl = self.stores.control
        rules = ctl.query("SELECT * FROM harness_rules WHERE workspace_id = ? AND status IN ('active', 'retired')",
                          [ws])
        effects = []
        for r in rules:
            e = rule_effect(ctl, ws, r)
            if e is not None:
                effects.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "text": r["text"], **e})
        harness = {"in_force": sum(1 for r in rules if r["status"] == "active"),
                   "working": sum(1 for e in effects if e["status"] == "active" and e["verdict"] == "working"),
                   "ineffective": sum(1 for e in effects if e["status"] == "active" and e["ineffective"]),
                   "rules": effects}

        mem = db.query("SELECT trust, (disputed_at IS NOT NULL) AS disputed, count(*) AS n FROM memory WHERE "
                       "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()) GROUP BY 1, 2")
        by_trust: dict[str, int] = {}
        disputed = 0
        for m in mem:
            if m["disputed"]:
                disputed += m["n"]
            else:
                by_trust[m["trust"]] = by_trust.get(m["trust"], 0) + m["n"]
        memory = {"recallable": sum(by_trust.values()), "by_trust": by_trust, "held_back_disputed": disputed}
        return {"workspace_id": ws, "days": days, "answers": answers, "workflows": workflows, "harness": harness,
                "memory": memory}
