# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Living specifications for analytical projects.

A spec turns an ambiguous request into structured, versioned requirements: objective, stakeholders, the governed
metrics and datasets involved, deliverables, constraints, assumptions, success measures, required approvals, and
requirements with priorities (must, should, could) and acceptance criteria.

Requirements are traced to the work that satisfies them: tasks, generated artifacts, registered assets and
decisions. A requirement is *met* by evidence, not by declaration: a linked task that finished verified, a linked
artifact or asset that exists and is current. A person can also mark one met (with a note) or waive it (an
administrator, with a reason). A spec cannot be completed while a must-have is open; the refusal lists what is
missing.

Specs change. Revising one creates a new version. A requirement whose text is unchanged keeps its links and status.
A requirement whose text changed keeps its links but needs its evidence checked again, and its old wording stays in
the history. A removed requirement is kept as *dropped*. Valid work is preserved, affected work is flagged, and
every version stays readable.

Drafting from a request is rule-based and reads only the request, the workspace's approved metrics and glossary,
and the catalog's dataset names, so a draft never invents data the workspace does not have.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .security import Guard, Principal

PRIORITIES = ("must", "should", "could")
LINK_KINDS = ("task", "artifact", "asset", "decision")

_DELIVERABLES = [
    (re.compile(r"\bdashboards?\b", re.I), "dashboard", "A dashboard"),
    (re.compile(r"\b(deck|slides?|presentation)\b", re.I), "deck", "A slide deck"),
    (re.compile(r"\b(workbook|spreadsheet|excel|xlsx)\b", re.I), "workbook", "A workbook"),
    (re.compile(r"\b(report|brief(ing)?|memo|summary|write[- ]?up)\b", re.I), "report", "A written report"),
    (re.compile(r"\b(monitor|alert|watch|notify)\b", re.I), "monitor", "A monitor that alerts on changes"),
    (re.compile(r"\b(forecast|projection|predict)\w*\b", re.I), "forecast", "A forecast"),
    (re.compile(r"\b(weekly|monthly|daily|every (week|month|day|quarter))\b", re.I), "schedule", "A recurring scheduled run"),
]
_STAKEHOLDER = re.compile(r"\b(?:for|to|with) (?:the |our )?(CFO|CEO|COO|CTO|board|leadership|executives?|finance|sales|operations|"
                          r"marketing|HR|procurement|investors?|management|[A-Z][a-z]+ team)\b")
_MENTION = re.compile(r"@?\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_DEADLINE = re.compile(r"\b(?:by|before|due) ((?:mon|tues|wednes|thurs|fri|satur|sun)day|end of (?:the )?(?:week|month|quarter|year)|"
                       r"\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2})\b", re.I)
_ONLY = re.compile(r"\b(?:only|just|excluding|except|without) ([^,.;]+)", re.I)
_SENT = re.compile(r"(?<=[.;!?])\s+|\n+|\s+(?:and then|then|also)\s+", re.I)
_MUST = re.compile(r"\b(must|need(s|ed)? to|has to|required?|critical|essential)\b", re.I)
_COULD = re.compile(r"\b(could|nice to have|if possible|optionally|maybe|ideally)\b", re.I)


class SpecIncomplete(ValueError):
    def __init__(self, unmet: list[dict[str, Any]]) -> None:
        super().__init__("still open: " + "; ".join(f"{r['id']} {r['text'][:80]}" for r in unmet))
        self.unmet = unmet


class SpecService:
    def __init__(self, stores, guard: Guard, *, semantic=None, runtime=None, artifacts=None, assets=None) -> None:
        self.stores = stores
        self.guard = guard
        self.semantic = semantic
        self.runtime = runtime
        self.artifacts = artifacts
        self.assets = assets

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- drafting -------------------------------------------------------------------------------

    def draft(self, actor: Principal, ws: str, request: str) -> dict[str, Any]:
        """A proposed spec body from a request, grounded in the workspace's approved metrics and datasets. Nothing
        is saved; ``create`` saves it (as edited)."""
        self.guard.require(actor, ws, "workspace.read")
        text = " ".join((request or "").split())
        if len(text) < 10:
            raise ValueError("describe what you need in a sentence or more")
        resolved = self.semantic.resolve(actor, ws, text) if self.semantic else {"metrics": [], "ambiguous": {}}
        metrics = []
        datasets: set[str] = set()
        for name in resolved["metrics"]:
            spec = self.semantic.get(actor, ws, name)
            metrics.append({"name": name, "title": spec.title})
            datasets.add(spec.dataset)
        names = {r["name"] for r in self._db(ws).query("SELECT name FROM datasets")}
        words = set(re.findall(r"[a-z_][a-z0-9_]+", text.lower()))
        datasets |= names & words
        deliverables = [{"kind": k, "text": label} for pat, k, label in _DELIVERABLES if pat.search(text)] or \
                       [{"kind": "answer", "text": "A verified answer"}]
        stakeholders = sorted({m.group(1) for m in _STAKEHOLDER.finditer(text)} | {m.group(0).lstrip("@") for m in _MENTION.finditer(text)})
        constraints = [f"Deadline: {m.group(1)}" for m in _DEADLINE.finditer(text)] + [f"Scope: only {m.group(1).strip()}" for m in _ONLY.finditer(text)]
        assumptions = []
        for term, options in resolved.get("ambiguous", {}).items():
            assumptions.append(f"\"{term}\" could mean {' or '.join(options)}; confirm which.")
        if not metrics:
            assumptions.append("No approved metric matched the request; a metric may need to be defined first.")
        requirements = []
        for i, clause in enumerate(c.strip(" ,") for c in _SENT.split(text) if len(c.strip()) > 8):
            prio = "could" if _COULD.search(clause) else "must" if (_MUST.search(clause) or i == 0) else "should"
            requirements.append({"id": f"R{len(requirements) + 1}", "text": clause[0].upper() + clause[1:], "priority": prio,
                                 "acceptance": self._acceptance(clause, metrics, deliverables)})
        approvals = []
        if any(d["kind"] in ("schedule", "monitor") for d in deliverables):
            approvals.append("Activating a schedule or monitor needs an administrator's approval.")
        if any("metric may need" in a for a in assumptions):
            approvals.append("A new metric definition needs an administrator's approval.")
        return {"objective": requirements[0]["text"] if requirements else text, "request": text, "stakeholders": stakeholders,
                "metrics": metrics, "datasets": sorted(datasets), "deliverables": deliverables, "constraints": constraints,
                "assumptions": assumptions, "success_metrics": [f"{m['title']} answered and verified" for m in metrics] or
                ["The question is answered and verified"], "approvals_required": approvals, "requirements": requirements}

    @staticmethod
    def _acceptance(clause: str, metrics: list[dict[str, Any]], deliverables: list[dict[str, Any]]) -> list[str]:
        out = []
        low = clause.lower()
        for m in metrics:
            if m["name"].replace("_", " ") in low or m["title"].lower() in low:
                out.append(f"{m['title']} is computed from its approved definition and passes validation")
        for pat, _kind, label in _DELIVERABLES:
            if pat.search(clause):
                out.append(f"{label} exists and is current")
        return out or ["A linked task finishes with a verified answer"]

    # -- create, read, revise -------------------------------------------------------------------

    def create(self, actor: Principal, ws: str, title: str, body: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.create")
        body = self._normalize(body)
        sid = "spec_" + uuid.uuid4().hex[:10]
        db = self._db(ws)

        def tx(c):
            c.execute("INSERT INTO project_specs (id, title, status, version, created_by) VALUES (?, ?, 'active', 1, ?)", [sid, title[:200], actor.user_id])
            c.execute("INSERT INTO spec_versions (spec_id, version, body, note, created_by) VALUES (?, 1, ?, 'created', ?)",
                      [sid, json.dumps(body), actor.user_id])

        db.write(tx)
        self.guard.audit(actor, ws, "spec.create", sid, "ok", {"requirements": len(body["requirements"])})
        return self.get(actor, ws, sid)

    @staticmethod
    def _normalize(body: dict[str, Any]) -> dict[str, Any]:
        reqs = body.get("requirements") or []
        if not reqs:
            raise ValueError("a spec needs at least one requirement")
        seen = set()
        out = []
        for i, r in enumerate(reqs):
            rid = str(r.get("id") or f"R{i + 1}")
            if rid in seen:
                raise ValueError(f"requirement id {rid} appears twice")
            seen.add(rid)
            prio = r.get("priority", "should")
            if prio not in PRIORITIES:
                raise ValueError(f"priority is one of {', '.join(PRIORITIES)}")
            text = str(r.get("text") or "").strip()
            if not text:
                raise ValueError(f"requirement {rid} has no text")
            out.append({"id": rid, "text": text, "priority": prio, "acceptance": [str(a) for a in r.get("acceptance") or []],
                        "links": [lk for lk in r.get("links") or [] if lk.get("kind") in LINK_KINDS],
                        "manual": r.get("manual"), "recheck": bool(r.get("recheck"))})
        return {**{k: body.get(k) or [] for k in ("stakeholders", "metrics", "datasets", "deliverables", "constraints", "assumptions",
                                                    "success_metrics", "approvals_required")},
                "objective": body.get("objective") or out[0]["text"], "request": body.get("request"), "requirements": out,
                "dropped": body.get("dropped") or []}

    def _row(self, ws: str, sid: str) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM project_specs WHERE id = ?", [sid])
        if not rows:
            raise KeyError(sid)
        return rows[0]

    def _body(self, ws: str, sid: str, version: int) -> dict[str, Any]:
        return json.loads(self._db(ws).scalar("SELECT body FROM spec_versions WHERE spec_id = ? AND version = ?", [sid, version]))

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        out = []
        for r in self._db(ws).query("SELECT * FROM project_specs ORDER BY updated_at DESC"):
            ev = self._evaluate(actor, ws, self._body(ws, r["id"], r["version"]))
            out.append({**r, "progress": ev["progress"]})
        return out

    def get(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", sid)
        r = self._row(ws, sid)
        body = self._body(ws, sid, r["version"])
        return {**r, "body": body, **self._evaluate(actor, ws, body)}

    def history(self, actor: Principal, ws: str, sid: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read", sid)
        self._row(ws, sid)
        rows = self._db(ws).query("SELECT version, body, note, created_by, created_at FROM spec_versions WHERE spec_id = ? ORDER BY version DESC", [sid])
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def _write_version(self, actor: Principal, ws: str, sid: str, body: dict[str, Any], note: str, *, status: str | None = None) -> int:
        db = self._db(ws)
        r = self._row(ws, sid)
        version = r["version"] + 1

        def tx(c):
            c.execute("INSERT INTO spec_versions (spec_id, version, body, note, created_by) VALUES (?, ?, ?, ?, ?)",
                      [sid, version, json.dumps(body), note[:500], actor.user_id])
            c.execute("UPDATE project_specs SET version = ?, status = coalesce(?, status), updated_at = now() WHERE id = ?", [version, status, sid])

        db.write(tx)
        return version

    def _editable(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.control", sid)
        r = self._row(ws, sid)
        if r["status"] in ("complete", "cancelled"):
            raise ValueError(f"this spec is {r['status']}")
        return r

    def revise(self, actor: Principal, ws: str, sid: str, changes: dict[str, Any], *, note: str) -> dict[str, Any]:
        """A new version. Unchanged requirements keep links and status; changed ones keep links but are flagged for
        re-verification; removed ones are kept as dropped."""
        r = self._editable(actor, ws, sid)
        if not note or len(note.strip()) < 3:
            raise ValueError("say what changed and why")
        old = self._body(ws, sid, r["version"])
        new = self._normalize({**old, **changes})
        before = {q["id"]: q for q in old["requirements"]}
        affected, added = [], []
        for q in new["requirements"]:
            prev = before.get(q["id"])
            if prev is None:
                added.append(q["id"])
                continue
            q["links"] = q["links"] or prev["links"]
            q["manual"] = prev.get("manual") if q["text"] == prev["text"] else None
            if q["text"] != prev["text"]:
                q["recheck"] = True
                q["previous_text"] = prev["text"]
                affected.append(q["id"])
            else:
                q["recheck"] = prev.get("recheck", False)
        kept = {q["id"] for q in new["requirements"]}
        new["dropped"] = (old.get("dropped") or []) + [{**q, "dropped_in": r["version"] + 1} for q in old["requirements"] if q["id"] not in kept]
        version = self._write_version(actor, ws, sid, new, note)
        self.guard.audit(actor, ws, "spec.revise", sid, "ok", {"version": version, "affected": affected, "added": added,
                                                                  "dropped": [q["id"] for q in new["dropped"] if q.get("dropped_in") == version]})
        out = self.get(actor, ws, sid)
        out["change"] = {"version": version, "affected": affected, "added": added,
                         "dropped": [q["id"] for q in new["dropped"] if q.get("dropped_in") == version]}
        return out

    # -- traceability ---------------------------------------------------------------------------

    def link(self, actor: Principal, ws: str, sid: str, req_id: str, kind: str, object_id: str) -> dict[str, Any]:
        r = self._editable(actor, ws, sid)
        if kind not in LINK_KINDS:
            raise ValueError(f"kind is one of {', '.join(LINK_KINDS)}")
        self._check_object(actor, ws, kind, object_id)
        body = self._body(ws, sid, r["version"])
        q = self._req(body, req_id)
        if not any(lk["kind"] == kind and lk["id"] == object_id for lk in q["links"]):
            q["links"].append({"kind": kind, "id": object_id})
        q["recheck"] = False  # new evidence is attached; it is judged afresh below
        self._write_version(actor, ws, sid, body, f"linked {kind} {object_id} to {req_id}")
        self.guard.audit(actor, ws, "spec.link", sid, "ok", {"requirement": req_id, kind: object_id})
        return self.get(actor, ws, sid)

    def mark(self, actor: Principal, ws: str, sid: str, req_id: str, *, status: str, note: str) -> dict[str, Any]:
        """Mark a requirement met by hand (with a note) or waive it (administrators, with a reason)."""
        r = self._editable(actor, ws, sid)
        if status not in ("met", "waived", "open"):
            raise ValueError("status is met, waived or open")
        if status == "waived":
            self.guard.require(actor, ws, "workspace.admin", sid)
        if status != "open" and (not note or len(note.strip()) < 3):
            raise ValueError("add a note saying what shows it")
        body = self._body(ws, sid, r["version"])
        q = self._req(body, req_id)
        q["manual"] = None if status == "open" else {"status": status, "note": note.strip(), "by": actor.user_id}
        q["recheck"] = False
        self._write_version(actor, ws, sid, body, f"{req_id} marked {status}")
        self.guard.audit(actor, ws, "spec.mark", sid, "ok", {"requirement": req_id, "status": status})
        return self.get(actor, ws, sid)

    @staticmethod
    def _req(body: dict[str, Any], req_id: str) -> dict[str, Any]:
        for q in body["requirements"]:
            if q["id"] == req_id:
                return q
        raise KeyError(req_id)

    def _check_object(self, actor: Principal, ws: str, kind: str, object_id: str) -> None:
        if kind == "task":
            t = self.runtime.get(actor, object_id)
            if t["workspace_id"] != ws:
                raise KeyError(object_id)
        elif kind == "artifact":
            meta, _ = self.artifacts.get(actor, object_id)
            if meta["workspace_id"] != ws:
                raise KeyError(object_id)
        elif kind == "asset":
            self.assets.get(actor, ws, object_id)
        elif kind == "decision":
            if not self._db(ws).scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = 'decisions'") or \
                    not self._db(ws).scalar("SELECT count(*) FROM decisions WHERE id = ?", [object_id]):
                raise KeyError(object_id)

    def _evidence(self, actor: Principal, ws: str, link: dict[str, Any]) -> tuple[bool, str]:
        """Whether one linked object satisfies a requirement, and why, from its own current state."""
        kind, oid = link["kind"], link["id"]
        try:
            if kind == "task":
                t = self.stores.control.query("SELECT status, result->>'$.status' AS outcome, title FROM tasks WHERE id = ? AND workspace_id = ?", [oid, ws])
                if not t:
                    return False, f"task {oid} no longer exists"
                ok = t[0]["status"] == "done" and t[0]["outcome"] == "complete"
                return ok, f"task \"{t[0]['title'][:60]}\" " + ("finished verified" if ok else f"is {t[0]['outcome'] or t[0]['status']}")
            if kind == "artifact":
                a = self.stores.control.query("SELECT kind FROM artifacts WHERE id = ? AND workspace_id = ?", [oid, ws])
                return bool(a), (f"{a[0]['kind']} artifact exists" if a else f"artifact {oid} no longer exists")
            if kind == "asset":
                a = self._db(ws).query("SELECT title, health, status FROM assets WHERE id = ?", [oid])
                if not a:
                    return False, f"asset {oid} no longer exists"
                ok = a[0]["status"] == "active" and a[0]["health"] == "current"
                return ok, f"\"{a[0]['title'][:60]}\" is {a[0]['health'] if a[0]['status'] == 'active' else a[0]['status']}"
            if kind == "decision":
                d = self._db(ws).query("SELECT status FROM decisions WHERE id = ?", [oid])
                return bool(d), (f"decision is {d[0]['status']}" if d else f"decision {oid} no longer exists")
        except Exception as exc:  # noqa: BLE001 - a missing table or object is evidence that is not there
            return False, f"{kind} {oid} could not be checked ({str(exc)[:60]})"
        return False, "unknown link"

    def _evaluate(self, actor: Principal, ws: str, body: dict[str, Any]) -> dict[str, Any]:
        reqs = []
        for q in body["requirements"]:
            evidence = [{"link": lk, "satisfies": ok, "why": why} for lk in q["links"] for ok, why in [self._evidence(actor, ws, lk)]]
            manual = q.get("manual")
            if manual and manual["status"] == "waived":
                status, why = "waived", manual["note"]
            elif q.get("recheck"):
                status, why = "recheck", "the requirement changed; confirm the linked work still satisfies it, or link new work"
            elif any(e["satisfies"] for e in evidence):
                status, why = "met", next(e["why"] for e in evidence if e["satisfies"])
            elif manual and manual["status"] == "met":
                status, why = "met", f"marked met: {manual['note']}"
            elif evidence:
                status, why = "in_progress", evidence[-1]["why"]
            else:
                status, why = "open", "no work linked yet"
            reqs.append({**q, "status": status, "why": why, "evidence": evidence})
        musts = [q for q in reqs if q["priority"] == "must"]
        done = [q for q in reqs if q["status"] in ("met", "waived")]
        unmet = [q for q in musts if q["status"] not in ("met", "waived")]
        return {"requirements": reqs, "unmet_must": [{"id": q["id"], "text": q["text"], "status": q["status"]} for q in unmet],
                "progress": {"met": len(done), "total": len(reqs), "must_open": len(unmet)}, "can_complete": not unmet}

    def complete(self, actor: Principal, ws: str, sid: str, *, note: str = "") -> dict[str, Any]:
        r = self._editable(actor, ws, sid)
        body = self._body(ws, sid, r["version"])
        ev = self._evaluate(actor, ws, body)
        if not ev["can_complete"]:
            self.guard.audit(actor, ws, "spec.complete", sid, "refused", {"unmet": [q["id"] for q in ev["unmet_must"]]})
            raise SpecIncomplete(ev["unmet_must"])
        self._write_version(actor, ws, sid, body, note or "completed", status="complete")
        self.guard.audit(actor, ws, "spec.complete", sid, "ok")
        return self.get(actor, ws, sid)

    def cancel(self, actor: Principal, ws: str, sid: str, *, note: str) -> dict[str, Any]:
        r = self._editable(actor, ws, sid)
        self._write_version(actor, ws, sid, self._body(ws, sid, r["version"]), note or "cancelled", status="cancelled")
        self.guard.audit(actor, ws, "spec.cancel", sid, "ok")
        return self.get(actor, ws, sid)
