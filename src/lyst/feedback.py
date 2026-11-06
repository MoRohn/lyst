# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A person's verdict on an answer, and what Lyst stops or starts reusing because of it.

Lyst's own checks decide whether an answer is *verified*; only a person can say it is *wrong*. A verdict is one of
``correct``, ``incorrect`` or ``partly``, with an optional note and an optional correction.

**What a verdict changes** (only when the person may write memory, an editor or above; a viewer's verdict is
recorded and counted, and changes nothing Lyst reuses):

- ``incorrect``: the analysis is *disputed*. Its episodic memory leaves recall (an approved one stays, for an admin to
  review), it is no longer cited as a prior answer, and the compiled workflow it ran on or verified goes stale with the
  reason. A later verified run with the same steps on the same data is not recompiled; a different plan, or changed
  data or definitions, compiles a new version as usual.
- ``correct``: the analysis is *confirmed* and its episodic memory is raised from ``observed`` to ``user_validated``.
- ``partly``: recorded only.
- A correction becomes a ``user_validated`` memory linked to the analysis, so context recalls it next time.

**Reversible.** Withdrawing a verdict undoes its effects. Effects are not applied step by step but reconciled from the
set of verdicts still standing (``reconcile``), so a crash between writes is repaired by the next verdict, withdrawal
or reconcile, and two verdicts on one analysis can never leave it half-applied.

Nothing here edits code, prompts or rules: a demotion is automatic and undoable; a promotion of knowledge to
``approved`` stays an admin's step (``memory.promote``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .security import Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.feedback")

VERDICTS = ("correct", "incorrect", "partly")
DISPUTED = "a person marked an answer it gave as incorrect"


def disputed_reason(analysis_id: str) -> str:
    return f"{DISPUTED} (analysis {analysis_id})"


def _loads(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


class FeedbackService:
    def __init__(self, stores: Stores, guard: Guard, memory) -> None:
        self.stores, self.guard, self.memory = stores, guard, memory

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    def _analysis(self, ws: str, analysis_id: str) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT id, task_id, question, evidence, status FROM analyses WHERE id = ?", [analysis_id])
        if not rows:
            raise KeyError(f"no analysis {analysis_id}")
        return rows[0]

    # -- giving and withdrawing ----------------------------------------------------------------------------------------

    def give(self, actor: Principal, ws: str, analysis_id: str, verdict: str, *, note: str | None = None,
             correction: str | None = None) -> dict[str, Any]:
        """Record a verdict (replacing this person's earlier one on the same analysis) and reconcile its effects."""
        self.guard.require(actor, ws, "analyst.ask", analysis_id)
        if verdict not in VERDICTS:
            raise ValueError("verdict must be correct, incorrect or partly")
        note = (note or "").strip()[:2000] or None
        correction = (correction or "").strip()[:4000] or None
        a = self._analysis(ws, analysis_id)
        governs = self.guard.can(actor, ws, "memory.write")
        correction_mid = None
        if correction and governs:
            correction_mid = self.memory.remember(
                actor, ws, "team", correction, trust="user_validated",
                provenance={"kind": "correction", "analysis_id": analysis_id, "question": a["question"]})
        fid = "fb_" + uuid.uuid4().hex[:12]
        db = self._db(ws)

        def tx(c):
            # one standing verdict per person and analysis: the newest replaces the earlier one
            c.execute("UPDATE answer_feedback SET withdrawn_at = now(), withdrawn_by = ? WHERE analysis_id = ? "
                      "AND given_by = ? AND withdrawn_at IS NULL", [actor.user_id, analysis_id, actor.user_id])
            c.execute("INSERT INTO answer_feedback (id, analysis_id, task_id, verdict, note, correction, "
                      "correction_memory_id, governs, given_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [fid, analysis_id, a["task_id"], verdict, note, correction, correction_mid, governs,
                       actor.user_id])

        db.write(tx)
        self.guard.audit(actor, ws, "answer.feedback", analysis_id, verdict, {"feedback_id": fid, "governs": governs})
        effects = self.reconcile(actor, ws, analysis_id)
        return {**self.get(actor, ws, fid), "analysis": effects}

    def withdraw(self, actor: Principal, ws: str, feedback_id: str) -> dict[str, Any]:
        """Take a verdict back. The person who gave it, or an admin, may."""
        fb = self.get(actor, ws, feedback_id)
        if fb["given_by"] != actor.user_id:
            self.guard.require(actor, ws, "memory.promote", feedback_id)
        if fb["withdrawn_at"] is None:
            self._db(ws).execute("UPDATE answer_feedback SET withdrawn_at = now(), withdrawn_by = ? WHERE id = ? "
                                 "AND withdrawn_at IS NULL", [actor.user_id, feedback_id])
            if fb.get("correction_memory_id"):
                try:
                    self.memory.revoke(actor, ws, fb["correction_memory_id"])
                except PermissionError:
                    log.info("feedback %s withdrawn; its correction memory stays (no permission to revoke)", feedback_id)
            self.guard.audit(actor, ws, "answer.feedback_withdraw", fb["analysis_id"], "ok", {"feedback_id": feedback_id})
        effects = self.reconcile(actor, ws, fb["analysis_id"])
        return {**self.get(actor, ws, feedback_id), "analysis": effects}

    # -- reading -------------------------------------------------------------------------------------------------------

    def get(self, actor: Principal, ws: str, feedback_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT * FROM answer_feedback WHERE id = ?", [feedback_id])
        if not rows:
            raise KeyError(f"no feedback {feedback_id}")
        return rows[0]

    def for_analysis(self, actor: Principal, ws: str, analysis_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        a = self._analysis(ws, analysis_id)
        rows = self._db(ws).query("SELECT * FROM answer_feedback WHERE analysis_id = ? ORDER BY created_at DESC",
                                  [analysis_id])
        return {"analysis_id": analysis_id, "status": a["status"] or "open", "feedback": rows}

    def summary(self, actor: Principal, ws: str, days: int = 90) -> dict[str, Any]:
        """Verdicts people gave in the last ``days``: counted from records, standing verdicts only."""
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query(
            f"SELECT verdict, governs, count(*) AS n FROM answer_feedback WHERE withdrawn_at IS NULL AND created_at > "
            f"now() - INTERVAL {max(1, int(days))} DAY GROUP BY 1, 2")
        by = {v: 0 for v in VERDICTS}
        for r in rows:
            by[r["verdict"]] += r["n"]
        judged = sum(by.values())
        status = {r["status"] or "open": r["n"] for r in self._db(ws).query(
            "SELECT status, count(*) AS n FROM analyses GROUP BY 1")}
        return {"days": days, "verdicts": by, "judged": judged,
                "judged_correct_rate": round(by["correct"] / judged, 3) if judged else None,
                "analyses": {"confirmed": status.get("confirmed", 0), "disputed": status.get("disputed", 0),
                             "open": status.get("open", 0)}}

    # -- effects -------------------------------------------------------------------------------------------------------

    def _workflows_of(self, ws: str, a: dict[str, Any]) -> list[dict[str, Any]]:
        """The compiled workflow versions an analysis ran on, or that its task compiled or verified."""
        db = self._db(ws)
        refs: dict[tuple[str, int], None] = {}
        used = ((_loads(a["evidence"]) or {}).get("workflow") or {}).get("used")
        if used and used.get("id"):
            refs[(used["id"], int(used["version"]))] = None
        if a["task_id"]:
            for r in db.query("SELECT id, version FROM workflows WHERE source_task = ? OR last_verified_task = ?",
                              [a["task_id"], a["task_id"]]):
                refs[(r["id"], int(r["version"]))] = None
        return [{"id": i, "version": v} for i, v in refs]

    def reconcile(self, actor: Principal, ws: str, analysis_id: str) -> dict[str, Any]:
        """Bring the analysis, its memory and its workflows in line with the verdicts still standing. Idempotent."""
        db = self._db(ws)
        a = self._analysis(ws, analysis_id)
        standing = db.query("SELECT verdict FROM answer_feedback WHERE analysis_id = ? AND withdrawn_at IS NULL "
                            "AND governs", [analysis_id])
        verdicts = {r["verdict"] for r in standing}
        status = "disputed" if "incorrect" in verdicts else ("confirmed" if "correct" in verdicts else "open")
        changed: list[str] = []
        if (a["status"] or "open") != status:
            db.execute("UPDATE analyses SET status = ? WHERE id = ?", [status, analysis_id])
            changed.append(f"analysis {status}")
        # the episodic memory of this answer ("Q: ... A: ...")
        for m in db.query("SELECT id, trust, disputed_at FROM memory WHERE layer = 'episodic' AND revoked_at IS NULL "
                          "AND provenance->>'analysis_id' = ?", [analysis_id]):
            if m["trust"] == "approved":
                continue  # an admin approved it; a dispute shows in the analysis, the admin decides on the memory
            if status == "disputed" and m["disputed_at"] is None:
                db.execute("UPDATE memory SET disputed_at = now() WHERE id = ?", [m["id"]])
                changed.append(f"memory {m['id']} left recall")
            elif status != "disputed" and m["disputed_at"] is not None:
                db.execute("UPDATE memory SET disputed_at = NULL WHERE id = ?", [m["id"]])
                changed.append(f"memory {m['id']} back in recall")
            want = "user_validated" if status == "confirmed" else "observed"
            if m["trust"] != want:
                db.execute("UPDATE memory SET trust = ? WHERE id = ?", [want, m["id"]])
                changed.append(f"memory {m['id']} {want}")
        # the workflows it ran on or verified
        wfs = self._workflows_of(ws, a)
        reason = disputed_reason(analysis_id)
        for ref in wfs:
            rows = db.query("SELECT status, invalid_reason FROM workflows WHERE id = ? AND version = ?",
                            [ref["id"], ref["version"]])
            if not rows:
                continue
            wf = rows[0]
            if status == "disputed" and wf["status"] == "active":
                db.execute("UPDATE workflows SET status = 'stale', invalid_reason = ? WHERE id = ? AND version = ? "
                           "AND status = 'active'", [reason, ref["id"], ref["version"]])
                changed.append(f"workflow {ref['id']} v{ref['version']} stale")
                self.guard.audit(actor, ws, "workflow.stale", f"{ref['id']}:v{ref['version']}", "ok",
                                 {"reason": reason})
            elif status != "disputed" and wf["status"] == "stale" and wf["invalid_reason"] == reason:
                other = self._other_dispute(ws, ref, analysis_id)
                if other:  # another standing dispute still holds it back
                    db.execute("UPDATE workflows SET invalid_reason = ? WHERE id = ? AND version = ?",
                               [disputed_reason(other), ref["id"], ref["version"]])
                else:
                    # back to active: match() re-checks its data and definitions before any reuse
                    db.execute("UPDATE workflows SET status = 'active', invalid_reason = NULL WHERE id = ? AND "
                               "version = ? AND status = 'stale'", [ref["id"], ref["version"]])
                    changed.append(f"workflow {ref['id']} v{ref['version']} active")
        if changed:
            self.guard.audit(actor, ws, "answer.reconcile", analysis_id, status, {"changed": changed})
        return {"id": analysis_id, "status": status, "workflows": wfs, "changed": changed}

    def _other_dispute(self, ws: str, ref: dict[str, Any], analysis_id: str) -> str | None:
        """Another disputed analysis that ran on or verified this workflow version, or None."""
        for a in self._db(ws).query("SELECT id, task_id, question, evidence, status FROM analyses WHERE status = "
                                    "'disputed' AND id <> ?", [analysis_id]):
            if ref in self._workflows_of(ws, a):
                return a["id"]
        return None
