# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Guided onboarding: from connected data to a governed workspace and a first verified answer.

Discovery is progressive and reads metadata only. It runs the catalog's discovery (insights per dataset and the
relationships between them), recognizes the business domains the metadata suggests (``domains``), and turns what
it found into *proposals*: candidate metrics, glossary terms, column policies for personal data, data contracts,
starter questions, and the usual sources that seem to be missing. Nothing it proposes is authoritative until an
administrator accepts it, and accepting goes through the same governed services an administrator would use by
hand (``SemanticService.define``, ``set_term``, ``DataService.set_column_policy``, ``ContractService.define``,
``TaskRuntime.submit``), so their permission checks, versioning and audit apply unchanged.

The workspace's business profile (adopted domain packages, the organization's own terms, fiscal calendar,
reporting period and objectives) is versioned: each change is a new version and the previous ones stay readable.

Progress toward first value is measured from records Lyst already keeps (workspace creation, first dataset version,
first analysis, first complete analysis, connector failures, approvals), so the numbers cannot drift from reality.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from . import domains
from .security import Guard, Principal
from .semantic import Measure, MetricSpec
from .store import Stores

log = logging.getLogger("lyst.onboarding")

KINDS = ("metric", "glossary", "policy", "contract", "question", "source")
PERIODS = ("week", "month", "quarter")
STALLED_DAYS = 7


def _now() -> datetime:
    return datetime.now(UTC)


def _seconds(a, b) -> int | None:
    return int((b - a).total_seconds()) if a and b else None


class OnboardingService:
    def __init__(self, stores: Stores, guard: Guard, *, catalog, semantic, contracts, data, knowledge=None, runtime=None) -> None:
        self.stores = stores
        self.guard = guard
        self.catalog = catalog
        self.semantic = semantic
        self.contracts = contracts
        self.data = data
        self.knowledge = knowledge
        self.runtime = runtime

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- what Lyst can see ----------------------------------------------------------------------

    def _metadata(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        """Datasets as the caller may see them: names, titles, descriptions and column metadata (no rows)."""
        out = []
        for d in self.catalog.overview(actor, ws):
            try:
                ins = self.catalog.insights(actor, ws, d["name"])
            except KeyError:
                ins = {}
            out.append({"name": d["name"], "title": d["title"], "description": d["description"], "owner": d["owner"],
                        "classification": d["classification"], "contract": d.get("contract"),
                        "columns": ins.get("columns", []), "time_column": (ins.get("continuity") or {}).get("column")})
        return out

    # -- discovery ------------------------------------------------------------------------------

    def discover(self, actor: Principal, ws: str) -> dict[str, Any]:
        """Run discovery and refresh the proposals. Proposals already accepted or dismissed are left as decided."""
        self.guard.require(actor, ws, "data.ingest")
        self.catalog.discover(actor, ws)
        meta = self._metadata(actor, ws)
        docs = self.knowledge.documents(actor, ws) if self.knowledge else []
        found = domains.recognize(meta, docs)
        adopted = set(self.profile(actor, ws)["spec"].get("domains", []))
        use = [d for d in found if d["strength"] == "strong" or d["id"] in adopted] or found[:1]
        proposals = self._proposals_for(ws, meta, use)
        db = self._db(ws)

        def tx(c):
            n = 0
            for p in proposals:
                row = c.execute("SELECT status FROM onboarding_proposals WHERE id = ?", [p["id"]]).fetchall()
                if row and row[0][0] != "proposed":
                    continue
                if row:
                    c.execute("UPDATE onboarding_proposals SET title = ?, domain = ?, payload = ?, evidence = ?, updated_at = now() "
                              "WHERE id = ?", [p["title"], p["domain"], json.dumps(p["payload"]), json.dumps(p["evidence"]), p["id"]])
                else:
                    c.execute("INSERT INTO onboarding_proposals (id, kind, title, domain, payload, evidence) VALUES (?, ?, ?, ?, ?, ?)",
                              [p["id"], p["kind"], p["title"], p["domain"], json.dumps(p["payload"]), json.dumps(p["evidence"])])
                    n += 1
            c.execute("INSERT INTO onboarding_discoveries (id, domains, datasets, proposals, created_by) VALUES (?, ?, ?, ?, ?)",
                      ["od_" + uuid.uuid4().hex[:12], json.dumps(found), len(meta), n, actor.user_id])
            return n

        new = db.write(tx)
        self.guard.audit(actor, ws, "onboarding.discover", None, "ok",
                         {"datasets": len(meta), "domains": [d["id"] for d in found], "new_proposals": new})
        return {"datasets": len(meta), "domains": found, "new_proposals": new,
                "proposals": self.proposals(actor, ws)}

    def _proposals_for(self, ws: str, meta: list[dict[str, Any]], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        db = self._db(ws)
        existing = {r["name"] for r in db.query("SELECT name FROM metrics")}
        built_from = set()
        for r in db.query("SELECT v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name AND v.version = m.current_version"):
            s = json.loads(r["spec"])
            me = s.get("measure") or {}
            built_from.add((s.get("dataset"), me.get("column"), me.get("agg")))
        terms = {r["term"] for r in db.query("SELECT term FROM glossary")}
        policies = {(r["dataset"], r["column_name"]) for r in db.query("SELECT dataset, column_name FROM column_policies")}
        out: list[dict[str, Any]] = []
        proposed_metrics: set[str] = set()
        for d in found:
            pkg = domains.get(d["id"])
            matched_any = False
            for m in pkg.metrics:
                if m.name in existing or m.name in proposed_metrics:
                    matched_any = matched_any or m.name in existing
                    continue
                hit = domains.match_metric(m, meta)
                if not hit or (hit["dataset"], hit["column"], m.agg) in built_from:
                    continue
                spec = MetricSpec(name=m.name, title=m.title, description=m.description or f"{m.title} ({pkg.title} starting point).",
                                  dataset=hit["dataset"], measure=Measure(agg=m.agg, column=hit["column"]), time_column=hit["time_column"],
                                  dimensions=hit["dimensions"], format=m.format, higher_is_better=m.higher_is_better)
                proposed_metrics.add(m.name)
                matched_any = True
                out.append({"id": f"metric:{m.name}", "kind": "metric", "domain": pkg.id, "title": f"Define {m.title} as {m.agg} of {hit['dataset']}.{hit['column']}",
                            "payload": spec.model_dump(), "evidence": {"package": f"{pkg.id}@{pkg.version}", "column": f"{hit['dataset']}.{hit['column']}",
                                                                      "semantic_type": hit["semantic_type"],
                                                                      "why": f"{hit['column']} is a {hit['semantic_type']} column whose name matches the {pkg.title} definition of {m.title}"}})
            for t in pkg.glossary:
                if t.term in terms:
                    continue
                maps = t.metric if t.metric and (t.metric in existing or t.metric in proposed_metrics) else None
                out.append({"id": f"glossary:{t.term}", "kind": "glossary", "domain": pkg.id, "title": f"Add the term \"{t.term}\"",
                            "payload": {"term": t.term, "definition": t.definition, "maps_to_metric": maps},
                            "evidence": {"package": f"{pkg.id}@{pkg.version}", "why": f"a common {pkg.title} term"}})
            if matched_any:
                for q in pkg.questions[:2]:
                    out.append({"id": f"question:{pkg.id}:{pkg.questions.index(q)}", "kind": "question", "domain": pkg.id,
                                "title": q, "payload": {"question": q},
                                "evidence": {"package": f"{pkg.id}@{pkg.version}", "why": f"a starter question for {pkg.title}"}})
            for s in d["missing_sources"]:
                out.append({"id": f"source:{pkg.id}:{s}", "kind": "source", "domain": pkg.id, "title": f"Connect {s} data",
                            "payload": {"source": s},
                            "evidence": {"package": f"{pkg.id}@{pkg.version}",
                                         "why": f"{pkg.title} work usually draws on {s}, and no dataset here looks like it"}})
        for ds in meta:
            for c in ds["columns"]:
                if c.get("semantic_type") == "personal_data" and (ds["name"], c["name"].lower()) not in policies:
                    out.append({"id": f"policy:{ds['name']}.{c['name']}", "kind": "policy", "domain": None,
                                "title": f"Limit {ds['name']}.{c['name']} to administrators",
                                "payload": {"dataset": ds["name"], "column": c["name"], "sensitivity": "personal", "min_role": "admin"},
                                "evidence": {"why": f"{c['name']} looks like personal data ({'; '.join(c.get('issues') or []) or 'profiled as personal'})"}})
            if self.contracts and not ds.get("contract"):
                out.append({"id": f"contract:{ds['name']}", "kind": "contract", "domain": None,
                            "title": f"Protect {ds['name']} with a data contract drafted from its current shape",
                            "payload": {"dataset": ds["name"]},
                            "evidence": {"why": "a contract checks every new version for missing columns, changed types and broken keys"}})
        return out

    # -- proposals ------------------------------------------------------------------------------

    def proposals(self, actor: Principal, ws: str, *, status: str | None = "proposed", kind: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql, args = "SELECT * FROM onboarding_proposals WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        order = "CASE kind " + " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(KINDS)) + " END"
        rows = self._db(ws).query(sql + f" ORDER BY {order}, id", args)
        for r in rows:
            for k in ("payload", "evidence", "result"):
                r[k] = json.loads(r[k]) if r.get(k) else None
            r["authority"] = "approved" if r["status"] == "accepted" else "proposed"
        return rows

    def _proposal(self, ws: str, pid: str) -> dict[str, Any]:
        rows = self._db(ws).query("SELECT * FROM onboarding_proposals WHERE id = ?", [pid])
        if not rows:
            raise KeyError(pid)
        p = rows[0]
        p["payload"] = json.loads(p["payload"])
        p["evidence"] = json.loads(p["evidence"])
        return p

    def accept(self, actor: Principal, ws: str, pid: str, edits: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a proposal real through the governed service it belongs to. ``edits`` override payload fields
        (rename a metric, pick another column, rephrase a definition) before it is applied."""
        self.guard.require(actor, ws, "workspace.read", pid)  # membership first; each kind's service checks its own permission
        p = self._proposal(ws, pid)
        if p["status"] != "proposed":
            raise ValueError(f"this proposal was already {p['status']}")
        payload = {**p["payload"], **(edits or {})}
        note = f"accepted from onboarding proposal {pid}" + (f" ({p['evidence'].get('package')})" if p["evidence"].get("package") else "")
        kind = p["kind"]
        if kind == "metric":
            spec = MetricSpec(**payload)
            version = self.semantic.define(actor, ws, spec, note=note)
            result = {"metric": spec.name, "version": version}
        elif kind == "glossary":
            self.semantic.set_term(actor, ws, payload["term"], payload["definition"], payload.get("maps_to_metric"), source=note)
            result = {"term": payload["term"]}
        elif kind == "policy":
            self.data.set_column_policy(actor, ws, payload["dataset"], payload["column"], payload["sensitivity"], payload["min_role"])
            result = {"dataset": payload["dataset"], "column": payload["column"], "min_role": payload["min_role"]}
        elif kind == "contract":
            from .contracts import ContractSpec  # noqa: PLC0415

            draft = self.contracts.draft(actor, ws, payload["dataset"])
            result = {"dataset": payload["dataset"], **self.contracts.define(actor, ws, payload["dataset"], ContractSpec(**draft), note=note)}
        elif kind == "question":
            if self.runtime is None:
                raise ValueError("no task runtime is available to run a question")
            result = {"task_id": self.runtime.submit(actor, ws, payload["question"])}
        else:
            raise ValueError("a source suggestion is a reminder: connect the source in Data, or dismiss the suggestion")
        self._decide(ws, pid, "accepted", actor, result)
        self.guard.audit(actor, ws, "onboarding.accept", pid, "ok", {"kind": kind})
        return {"id": pid, "status": "accepted", "result": result}

    def dismiss(self, actor: Principal, ws: str, pid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", pid)
        p = self._proposal(ws, pid)
        if p["status"] != "proposed":
            raise ValueError(f"this proposal was already {p['status']}")
        self._decide(ws, pid, "dismissed", actor, None)
        self.guard.audit(actor, ws, "onboarding.dismiss", pid, "ok")
        return {"id": pid, "status": "dismissed"}

    def _decide(self, ws: str, pid: str, status: str, actor: Principal, result: dict | None) -> None:
        self._db(ws).execute("UPDATE onboarding_proposals SET status = ?, decided_by = ?, decided_at = now(), updated_at = now(), "
                             "result = ? WHERE id = ? AND status = 'proposed'",
                             [status, actor.user_id, json.dumps(result, default=str) if result is not None else None, pid])

    # -- business profile -----------------------------------------------------------------------

    def profile(self, actor: Principal, ws: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT version, spec, created_by, created_at FROM domain_profiles ORDER BY version DESC LIMIT 1")
        if not rows:
            return {"version": 0, "spec": {"domains": [], "terminology": {}, "fiscal_year_start_month": 1,
                                           "reporting_period": "month", "objectives": []}}
        r = rows[0]
        r["spec"] = json.loads(r["spec"])
        return r

    def profile_history(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT version, spec, created_by, created_at FROM domain_profiles ORDER BY version DESC")
        for r in rows:
            r["spec"] = json.loads(r["spec"])
        return rows

    def set_profile(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        """A new profile version. Terms that map to an approved metric go into the glossary, so questions in the
        organization's own words resolve to its governed definitions."""
        self.guard.require(actor, ws, "workspace.admin", "profile")
        current = self.profile(actor, ws)
        spec = {**current["spec"], **{k: v for k, v in changes.items() if v is not None}}
        unknown = [d for d in spec["domains"] if d not in domains.packages()]
        if unknown:
            raise ValueError(f"unknown domain package(s): {', '.join(unknown)}")
        if not 1 <= int(spec["fiscal_year_start_month"]) <= 12:
            raise ValueError("fiscal_year_start_month is a month number from 1 to 12")
        if spec["reporting_period"] not in PERIODS:
            raise ValueError(f"reporting_period is one of {', '.join(PERIODS)}")
        objectives = [str(o).strip() for o in spec["objectives"] if str(o).strip()]
        if len(objectives) > 20 or any(len(o) > 300 for o in objectives):
            raise ValueError("up to 20 objectives of at most 300 characters")
        spec["objectives"] = objectives
        spec["packages"] = {d: domains.get(d).version for d in spec["domains"]}
        approved = {m.name for m in self.semantic.metrics(actor, ws)}
        terminology = {}
        for term, target in (spec.get("terminology") or {}).items():
            metric = target.get("metric") if isinstance(target, dict) else str(target)
            if metric not in approved:
                raise ValueError(f"the term {term!r} must map to an approved metric; {metric!r} is not one")
            terminology[term.lower().strip()] = {"metric": metric}
        spec["terminology"] = terminology
        db = self._db(ws)
        version = (db.scalar("SELECT max(version) FROM domain_profiles") or 0) + 1
        db.execute("INSERT INTO domain_profiles (version, spec, created_by) VALUES (?, ?, ?)", [version, json.dumps(spec), actor.user_id])
        for term, t in terminology.items():
            self.semantic.set_term(actor, ws, term, f"This organization's word for {t['metric']}.", t["metric"],
                                   source=f"business profile v{version}")
        self.guard.audit(actor, ws, "onboarding.profile", str(version), "ok", {"domains": spec["domains"]})
        return {"version": version, "spec": spec}

    # -- progress and first value ---------------------------------------------------------------

    def status(self, actor: Principal, ws: str) -> dict[str, Any]:
        """The guided steps, what is done, what to do next, and the first-value milestones."""
        self.guard.require(actor, ws, "workspace.read")
        db = self._db(ws)
        ctl = self.stores.control
        created = ctl.scalar("SELECT created_at FROM workspaces WHERE id = ?", [ws])
        first_dataset = db.scalar("SELECT min(created_at) FROM dataset_versions")
        discovered = db.scalar("SELECT min(created_at) FROM onboarding_discoveries")
        decided = db.scalar("SELECT min(decided_at) FROM onboarding_proposals WHERE status = 'accepted'")
        first_analysis = db.scalar("SELECT min(created_at) FROM analyses")
        first_verified = db.scalar("SELECT min(created_at) FROM analyses WHERE answer->>'status' = 'complete'")
        prof = self.profile(actor, ws)
        members = ctl.scalar("SELECT count(*) FROM memberships WHERE workspace_id = ?", [ws])
        pending = db.scalar("SELECT count(*) FROM onboarding_proposals WHERE status = 'proposed'")
        approved_metrics = db.scalar("SELECT count(*) FROM metrics WHERE status = 'approved'")
        steps = [
            {"key": "connect", "title": "Connect or upload data", "done": first_dataset is not None, "optional": False,
             "detail": "Upload a file or connect a database in Data."},
            {"key": "discover", "title": "Let Lyst study the data", "done": discovered is not None, "optional": False,
             "detail": "Reads names, types and shape, not rows, then suggests definitions."},
            {"key": "confirm", "title": "Confirm the business definitions", "done": bool(decided or approved_metrics or prof["spec"]["domains"]),
             "optional": False, "detail": f"{pending} suggestion{'s' if pending != 1 else ''} waiting." if pending else "Review suggested metrics and terms."},
            {"key": "people", "title": "Invite the team", "done": (members or 0) > 1, "optional": True,
             "detail": "Members see only what their role allows."},
            {"key": "objectives", "title": "Say what matters this year", "done": bool(prof["spec"]["objectives"]), "optional": True,
             "detail": "Objectives shape suggested questions and monitors."},
            {"key": "first_answer", "title": "Ask a first question", "done": first_verified is not None, "optional": False,
             "detail": "A starter question runs on the definitions you confirmed."},
        ]
        nxt = next((s["key"] for s in steps if not s["done"] and not s["optional"]), None)
        failures = ctl.scalar("SELECT count(*) FROM connection_events WHERE workspace_id = ? AND outcome <> 'ok'", [ws]) or 0
        approvals = ctl.scalar("SELECT count(*) FROM approvals WHERE workspace_id = ?", [ws]) or 0
        dismissed = db.scalar("SELECT count(*) FROM onboarding_proposals WHERE status = 'dismissed'") or 0
        last = max((t for t in (first_dataset, discovered, decided, first_analysis, first_verified) if t), default=created)
        state = "complete" if nxt is None else ("stalled" if last and (_now() - last).days >= STALLED_DAYS else "in_progress")
        milestones = {
            "workspace_created_at": created,
            "first_dataset_at": first_dataset, "seconds_to_first_dataset": _seconds(created, first_dataset),
            "first_analysis_at": first_analysis, "seconds_to_first_analysis": _seconds(created, first_analysis),
            "first_verified_at": first_verified, "seconds_to_first_verified": _seconds(created, first_verified),
            "configuration_errors": failures, "interventions": approvals + (db.scalar(
                "SELECT count(*) FROM onboarding_proposals WHERE status <> 'proposed'") or 0),
            "dismissed_suggestions": dismissed,
        }
        return {"state": state, "next": nxt, "steps": steps, "milestones": milestones,
                "profile_version": prof["version"], "pending_proposals": pending}

