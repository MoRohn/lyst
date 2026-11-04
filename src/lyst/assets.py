# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The asset registry: every deliverable Lyst produces as a durable, versioned organizational asset.

Reports (PDF briefing), workbooks, slide decks, Markdown briefings, dashboards and compiled workflows each get one
registry row with an identity, an owner, a visibility, a version, a validation status, the task and question that
produced it, and its **dependencies**: the dataset versions and metric definition versions it was built on. Each new
version (a rebuild, a refresh, a recompile) is kept in ``asset_versions``; earlier files stay where they were written,
and a dashboard's previous folder is kept beside it before a refresh overwrites it.

**Maintenance.** ``scan`` compares every active asset's dependencies with what is current:

* **broken**: a dataset it reads is gone, or the current version of one breaks its data contract;
* **invalidated**: a metric it uses was retired or redefined after it was built;
* **stale**: a dataset it reads has a newer version (the numbers are as of the older one);
* **current** otherwise.

Scans run after every ingest and every change to a definition, and on request. An asset that becomes broken or
invalidated raises one inbox notice for its owner, except when the only cause is a data contract breach, whose own
notice already lists every affected asset. ``propose`` says how to bring an asset up to date (refresh a
dashboard; re-run the question and rebuild a report; re-run and recompile a workflow) or that a person must fix the
cause first; ``repair`` does it. Repairs are verified like the original: a dashboard must pass its checks, a rebuilt
report comes from a verified analysis, and the asset gets a new version only when that holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from .notices import raise_notice
from .security import Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.assets")

REPORT_KINDS = {"pdf": "report", "xlsx": "workbook", "pptx": "presentation", "md": "briefing"}
KINDS = ("report", "workbook", "presentation", "briefing", "dashboard", "workflow", "data_model")
HEALTH = ("current", "stale", "invalidated", "broken", "repairing")


def _validation_of(result: dict[str, Any] | None) -> str:
    level = ((result or {}).get("confidence") or {}).get("level")
    return {"verified": "verified", "verified with warnings": "verified_with_warnings", "not verified": "not_verified"}.get(level, "unknown")


class AssetService:
    def __init__(self, stores: Stores, guard: Guard, *, semantic=None, runtime=None, artifacts=None, studio=None) -> None:
        self.stores = stores
        self.guard = guard
        self.semantic = semantic
        self.runtime = runtime
        self.artifacts = artifacts
        self.studio = studio
        self.transforms = None  # transforms.TransformService, set by the composition root

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- dependencies ---------------------------------------------------------------------------

    def _dataset_versions(self, ws: str) -> dict[str, int]:
        return {r["name"]: r["current_version"] for r in self._db(ws).query("SELECT name, current_version FROM datasets")}

    def _metric_versions(self, ws: str) -> dict[str, dict[str, Any]]:
        return {r["name"]: r for r in self._db(ws).query("SELECT name, current_version, status, replaced_by FROM metrics")}

    def _depends_from_result(self, ws: str, result: dict[str, Any]) -> dict[str, Any]:
        sup = result.get("supporting") or {}
        datasets = {s["dataset"]: int(s["version"]) for s in sup.get("sources", []) if s.get("dataset")}
        metrics = {}
        m = sup.get("metric") or {}
        name = (m.get("spec") or {}).get("name")
        if name:
            metrics[name] = int(m.get("version") or self._metric_versions(ws).get(name, {}).get("current_version") or 0)
        return {"datasets": datasets, "metrics": metrics}

    def _depends_from_metrics(self, ws: str, names: list[str]) -> dict[str, Any]:
        mv = self._metric_versions(ws)
        dv = self._dataset_versions(ws)
        metrics = {n: int(mv[n]["current_version"]) for n in names if n in mv}
        datasets = {}
        if self.semantic is not None:
            from .semantic import MetricSpec  # noqa: PLC0415

            for n in metrics:
                spec = self._db(ws).scalar("SELECT spec FROM metric_versions WHERE name = ? AND version = ?", [n, metrics[n]])
                if spec:
                    ds = MetricSpec.model_validate_json(spec).dataset
                    if ds in dv:
                        datasets[ds] = int(dv[ds])
        return {"datasets": datasets, "metrics": metrics}

    # -- registration (called by the services that produce assets) ------------------------------

    def _upsert(self, ws: str, actor: Principal, *, kind: str, ref_kind: str, ref_id: str, title: str, task_id: str | None,
                question: str | None, depends: dict[str, Any], validation: str, sha256: str | None, note: str,
                asset_id: str | None = None) -> dict[str, Any]:
        db = self._db(ws)
        if asset_id is None:
            found = db.query("SELECT id FROM assets WHERE ref_kind = ? AND ref_id = ?", [ref_kind, ref_id])
            asset_id = found[0]["id"] if found else None

        def tx(c):
            if asset_id:
                row = c.execute("SELECT version, owner FROM assets WHERE id = ?", [asset_id]).fetchone()
            else:
                row = None
            if row:
                version = row[0] + 1
                c.execute("UPDATE assets SET version = ?, ref_id = ?, title = ?, depends = ?, validation = ?, health = 'current', "
                          "health_reason = NULL, repair = NULL, updated_at = now(), checked_at = now(), task_id = coalesce(?, task_id) WHERE id = ?",
                          [version, ref_id, title, json.dumps(depends), validation, task_id, asset_id])
                aid = asset_id
            else:
                aid = "as_" + uuid.uuid4().hex[:12]
                version = 1
                c.execute("INSERT INTO assets (id, kind, title, owner, validation, version, ref_kind, ref_id, task_id, question, depends, "
                          "created_by, checked_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, now())",
                          [aid, kind, title, actor.user_id, validation, ref_kind, ref_id, task_id, question, json.dumps(depends), actor.user_id])
            c.execute("INSERT INTO asset_versions (asset_id, version, ref_id, sha256, depends, validation, note, created_by) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [aid, version, ref_id, sha256, json.dumps(depends), validation, note, actor.user_id])
            return aid, version

        aid, version = db.write(tx)
        self.guard.audit(actor, ws, "asset.version", aid, "ok", {"kind": kind, "version": version, "ref": ref_id})
        return {"asset_id": aid, "version": version}

    def on_artifact(self, actor: Principal, task: dict[str, Any], kind: str, artifact: dict[str, Any], sha256: str,
                    asset_id: str | None = None) -> dict[str, Any] | None:
        result = task.get("result") or {}
        try:
            return self._upsert(task["workspace_id"], actor, kind=REPORT_KINDS.get(kind, kind), ref_kind="artifact", ref_id=artifact["id"],
                                title=(result.get("question") or task.get("question") or artifact["name"])[:200], task_id=task["id"],
                                question=result.get("question") or task.get("question"), depends=self._depends_from_result(task["workspace_id"], result),
                                validation=_validation_of(result), sha256=sha256, note="rebuilt" if asset_id else "created", asset_id=asset_id)
        except Exception:  # noqa: BLE001 - the file is written; registration failure is logged, not raised
            log.exception("asset registration for artifact %s failed", artifact.get("id"))
            return None

    def on_dashboard(self, actor: Principal, ws: str, dashboard_id: str, design: dict[str, Any], checks: list[dict[str, Any]],
                     *, refreshed: bool = False, task_id: str | None = None) -> dict[str, Any] | None:
        ok = all(c["passed"] for c in checks if c.get("severity") == "error")
        warn = any(not c["passed"] for c in checks if c.get("severity") != "error")
        try:
            return self._upsert(ws, actor, kind="dashboard", ref_kind="dashboard", ref_id=dashboard_id, title=design.get("title", dashboard_id),
                                task_id=task_id, question=design.get("question"),
                                depends=self._depends_from_metrics(ws, [m["name"] for m in design.get("metrics", [])]),
                                validation="failed" if not ok else ("verified_with_warnings" if warn else "verified"),
                                sha256=hashlib.sha256(json.dumps(design, sort_keys=True, default=str).encode()).hexdigest(),
                                note="refreshed" if refreshed else "built")
        except Exception:  # noqa: BLE001
            log.exception("asset registration for dashboard %s failed", dashboard_id)
            return None

    def on_workflow(self, actor: Principal, ws: str, wf_id: str, version: int, title: str, deps: dict[str, Any], task_id: str | None,
                    question: str | None) -> dict[str, Any] | None:
        try:
            dv = self._dataset_versions(ws)
            depends = {"datasets": {d: int(dv[d]) for d in deps.get("datasets", {}) if d in dv},
                       "metrics": {m: int(v) for m, v in deps.get("metrics", {}).items()}}
            return self._upsert(ws, actor, kind="workflow", ref_kind="workflow", ref_id=wf_id, title=title, task_id=task_id, question=question,
                                depends=depends, validation="verified", sha256=None, note=f"compiled v{version}")
        except Exception:  # noqa: BLE001
            log.exception("asset registration for workflow %s failed", wf_id)
            return None

    def on_transform(self, actor: Principal, ws: str, name: str, *, failed: str | None = None) -> dict[str, Any] | None:
        """A transformation's derived dataset as a data model asset: a new version per successful run of a new definition,
        broken with the reason when a run fails (the last good dataset version stays in place)."""
        try:
            db = self._db(ws)
            t = db.query("SELECT version, title, description, inputs FROM transforms WHERE name = ?", [name])
            if not t:
                return None
            existing = db.query("SELECT id, version, depends FROM assets WHERE ref_kind = 'transform' AND ref_id = ?", [name])
            if failed:
                if existing:
                    db.execute("UPDATE assets SET health = 'broken', health_reason = ?, checked_at = now() WHERE id = ?",
                               [f"its last run failed: {failed}"[:500], existing[0]["id"]])
                return None
            dv = self._dataset_versions(ws)
            depends = {"datasets": {d: int(dv[d]) for d in json.loads(t[0]["inputs"]) if d in dv}, "metrics": {}}
            if existing and json.loads(existing[0]["depends"]).get("definition") == t[0]["version"]:
                # a refresh of the same definition: dependencies move forward, no new asset version
                db.execute("UPDATE assets SET depends = ?, health = 'current', health_reason = NULL, checked_at = now(), updated_at = now() "
                           "WHERE id = ?", [json.dumps({**depends, "definition": t[0]["version"]}), existing[0]["id"]])
                return {"asset_id": existing[0]["id"], "version": existing[0]["version"]}
            return self._upsert(ws, actor, kind="data_model", ref_kind="transform", ref_id=name, title=t[0]["title"], task_id=None,
                                question=t[0]["description"], depends={**depends, "definition": t[0]["version"]}, validation="verified",
                                sha256=None, note=f"definition v{t[0]['version']}")
        except Exception:  # noqa: BLE001
            log.exception("asset registration for transformation %s failed", name)
            return None

    # -- reading --------------------------------------------------------------------------------

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        out["depends"] = json.loads(out["depends"]) if isinstance(out.get("depends"), str) else out.get("depends")
        out["repair"] = json.loads(out["repair"]) if isinstance(out.get("repair"), str) else out.get("repair")
        return out

    def _visible(self, actor: Principal, ws: str, r: dict[str, Any]) -> bool:
        if r["visibility"] != "private" or r["owner"] == actor.user_id or self.guard.can(actor, ws, "workspace.admin"):
            return True
        return bool(self._db(ws).scalar("SELECT count(*) FROM asset_shares WHERE asset_id = ? AND user_id = ?", [r["id"], actor.user_id]))

    def list(self, actor: Principal, ws: str, *, kind: str | None = None, health: str | None = None,
             include_retired: bool = False) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        self.complete_repairs(ws)
        sql, params = "SELECT * FROM assets WHERE 1 = 1", []
        if not include_retired:
            sql += " AND status = 'active'"
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if health:
            sql += " AND health = ?"
            params.append(health)
        return [self._row(r) for r in self._db(ws).query(sql + " ORDER BY updated_at DESC", params) if self._visible(actor, ws, r)]

    def get(self, actor: Principal, ws: str, asset_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.read", asset_id)
        self.complete_repairs(ws)
        rows = self._db(ws).query("SELECT * FROM assets WHERE id = ?", [asset_id])
        if not rows or not self._visible(actor, ws, rows[0]):
            raise KeyError(asset_id)
        out = self._row(rows[0])
        versions = self._db(ws).query("SELECT version, ref_id, sha256, depends, validation, note, created_by, created_at FROM asset_versions "
                                      "WHERE asset_id = ? ORDER BY version DESC", [asset_id])
        for v in versions:
            v["depends"] = json.loads(v["depends"])
        out["versions"] = versions
        out["proposal"] = self._proposal(ws, out)
        return out

    # -- maintenance ----------------------------------------------------------------------------

    def _assess(self, ws: str, a: dict[str, Any], dv: dict[str, int], mv: dict[str, dict[str, Any]],
                contracts: dict[str, str]) -> tuple[str, str | None]:
        deps = a["depends"] or {}
        reasons: dict[str, list[str]] = {"broken": [], "invalidated": [], "stale": []}
        for ds, ver in (deps.get("datasets") or {}).items():
            if ds not in dv:
                reasons["broken"].append(f"dataset {ds} no longer exists")
                continue
            if contracts.get(ds) in ("breaking", "fail"):
                reasons["broken"].append(f"the current version of {ds} {'breaks' if contracts[ds] == 'breaking' else 'fails'} its data contract")
            if dv[ds] > ver:
                reasons["stale"].append(f"{ds} is now v{dv[ds]}; this was built on v{ver}")
        for m, ver in (deps.get("metrics") or {}).items():
            cur = mv.get(m)
            if not cur:
                reasons["broken"].append(f"metric {m} no longer exists")
            elif cur["status"] != "approved":
                reasons["invalidated"].append(f"metric {m} was retired" + (f" in favour of {cur['replaced_by']}" if cur["replaced_by"] else ""))
            elif ver and cur["current_version"] != ver:
                reasons["invalidated"].append(f"the definition of {m} changed (v{ver} → v{cur['current_version']})")
        if a["kind"] == "workflow":
            st = self._db(ws).query("SELECT status, invalid_reason FROM workflows WHERE id = ? ORDER BY version DESC LIMIT 1", [a["ref_id"]])
            if st and st[0]["status"] == "stale":
                reasons["invalidated"].append(st[0]["invalid_reason"] or "the workflow is stale")
        if a["kind"] == "data_model":
            st = self._db(ws).query("SELECT status, last_error FROM transforms WHERE name = ?", [a["ref_id"]])
            if not st or st[0]["status"] == "retired":
                reasons["broken"].append("its transformation was retired")
            elif st[0]["status"] == "failed":
                reasons["broken"].append(f"its last run failed: {st[0]['last_error']}")
        if a["kind"] == "dashboard":
            st = self._db(ws).scalar("SELECT status FROM dashboards WHERE id = ?", [a["ref_id"]])
            if st == "failed":
                reasons["broken"].append("its last build or refresh failed its checks")
        for state in ("broken", "invalidated", "stale"):
            if reasons[state]:
                return state, "; ".join(reasons[state])
        return "current", None

    def scan(self, ws: str, actor: Principal | None = None) -> list[dict[str, Any]]:
        """Re-assess every active asset; returns the ones whose health changed. System work: callers check access."""
        self.complete_repairs(ws)
        db = self._db(ws)
        dv, mv = self._dataset_versions(ws), self._metric_versions(ws)
        contracts = {}
        for r in db.query("SELECT c.dataset, c.outcome FROM contract_checks c JOIN datasets d ON d.name = c.dataset AND d.current_version = c.dataset_version "
                          "JOIN data_contracts k ON k.dataset = c.dataset AND k.version = c.contract_version AND k.status = 'active' "
                          "QUALIFY row_number() OVER (PARTITION BY c.dataset ORDER BY c.checked_at DESC) = 1"):
            contracts[r["dataset"]] = r["outcome"]
        changed = []
        for r in db.query("SELECT * FROM assets WHERE status = 'active' AND health <> 'repairing'"):
            a = self._row(r)
            health, reason = self._assess(ws, a, dv, mv, contracts)
            if health != a["health"] or reason != a["health_reason"]:
                db.execute("UPDATE assets SET health = ?, health_reason = ?, checked_at = now() WHERE id = ?", [health, reason, a["id"]])
                changed.append({"asset_id": a["id"], "kind": a["kind"], "title": a["title"], "from": a["health"], "to": health, "reason": reason})
                # a contract breach already raised one notice listing every affected asset; do not repeat it per asset
                if health in ("broken", "invalidated") and not (health == "broken" and all("data contract" in x for x in reason.split("; "))):
                    raise_notice(self.stores, ws, source=f"asset:{a['id']}", kind=f"asset_{health}", label=a["title"][:120],
                                 summary=f"The {a['kind']} \"{a['title'][:120]}\" is {health}: {reason}. "
                                         + (self._proposal(ws, {**a, "health": health, "health_reason": reason})["text"]),
                                 priority=7.0 if health == "broken" else 5.0, period=f"v{a['version']}",
                                 dedupe_key=f"asset:{a['id']}:v{a['version']}:{health}:{reason}"[:400], recipients={a["owner"]})
            else:
                db.execute("UPDATE assets SET checked_at = now() WHERE id = ?", [a["id"]])
        return changed

    def scan_now(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        return self.scan(ws, actor)

    def after_ingest(self, actor: Principal, ws: str, result: dict[str, Any]) -> None:
        if not result.get("unchanged"):
            try:
                self.scan(ws, actor)
            except Exception:  # noqa: BLE001 - advisory
                log.exception("asset scan after ingest failed")

    def after_definition(self, actor: Principal, ws: str, name: str) -> None:
        try:
            self.scan(ws, actor)
        except Exception:  # noqa: BLE001 - advisory
            log.exception("asset scan after a definition change failed")

    def affected_by(self, actor: Principal, ws: str, *, dataset: str | None = None, metric: str | None = None) -> list[dict[str, Any]]:
        """Registered assets built on a dataset or a metric."""
        return [a for a in self.list(actor, ws)
                if (dataset and dataset in (a["depends"].get("datasets") or {})) or (metric and metric in (a["depends"].get("metrics") or {}))]

    # -- repair ---------------------------------------------------------------------------------

    def _proposal(self, ws: str, a: dict[str, Any]) -> dict[str, Any]:
        reason = a.get("health_reason") or ""
        if a["health"] == "current":
            return {"action": None, "automatic": False, "text": "Up to date."}
        if a["health"] == "repairing":
            return {"action": None, "automatic": False, "text": "A repair is running."}
        if "no longer exists" in reason or "contract" in reason or "failed its checks" in reason and a["kind"] != "dashboard":
            return {"action": None, "automatic": False,
                    "text": "Needs a person first: fix the source or the definition named above, then repair this."}
        if a["kind"] == "data_model":
            if "failed" in reason or "retired" in reason:
                return {"action": None, "automatic": False,
                        "text": "Needs a person first: the transformation's query no longer runs; fix it (a new version) and it runs again."}
            return {"action": "rerun", "automatic": True, "text": "Run the transformation again on the current inputs; its checks must pass."}
        if a["kind"] == "dashboard":
            return {"action": "refresh", "automatic": True, "text": "Refresh it: Lyst re-materializes its data with the current definitions and verifies every page."}
        if a["kind"] == "workflow":
            return {"action": "recompile", "automatic": True, "text": "Re-run its question: a verified run recompiles the workflow on the current definitions."}
        return {"action": "rebuild", "automatic": True,
                "text": f"Rebuild it: Lyst re-runs \"{(a.get('question') or '')[:80]}\" on current data and writes a new version once the answer is verified."}

    def propose(self, actor: Principal, ws: str, asset_id: str) -> dict[str, Any]:
        a = self.get(actor, ws, asset_id)
        return {"asset_id": asset_id, "health": a["health"], "reason": a["health_reason"], **a["proposal"]}

    def repair(self, actor: Principal, ws: str, asset_id: str) -> dict[str, Any]:
        """Bring an asset up to date. Dashboards refresh now (and keep their previous folder); reports and workflows re-run
        their question as a task, and the new version is written when that task finishes verified."""
        self.guard.require(actor, ws, "task.control", asset_id)
        a = self.get(actor, ws, asset_id)
        if a["status"] != "active":
            raise ValueError("this asset is retired")
        prop = a["proposal"]
        if not prop["automatic"]:
            raise ValueError(prop["text"])
        db = self._db(ws)
        if a["kind"] == "data_model":
            out = self.transforms.run(actor, ws, a["ref_id"], trigger="repair", full=True)
            self.scan(ws, actor)
            return {"asset_id": asset_id, "action": "rerun", "status": out["outcome"], "version": self.get(actor, ws, asset_id)["version"]}
        if a["kind"] == "dashboard":
            path = db.scalar("SELECT path FROM dashboards WHERE id = ?", [a["ref_id"]])
            if path and Path(path).exists():
                keep = Path(path).parent / f"{Path(path).name}.versions" / f"v{a['version']}"
                if not keep.exists():
                    shutil.copytree(path, keep, ignore=shutil.ignore_patterns("__pycache__", ".cache"))
            out = self.studio.refresh(actor, ws, a["ref_id"])
            self.scan(ws, actor)
            return {"asset_id": asset_id, "action": "refresh", "status": out["status"], "checks": out["checks"],
                    "version": self.get(actor, ws, asset_id)["version"]}
        if not a.get("question"):
            raise ValueError("this asset does not record the question that produced it, so it cannot be rebuilt automatically")
        task_id = self.runtime.submit(actor, ws, a["question"])
        artifact_kind = next((k for k, v in REPORT_KINDS.items() if v == a["kind"]), None)
        db.execute("UPDATE assets SET health = 'repairing', repair = ?, updated_at = now() WHERE id = ?",
                   [json.dumps({"task_id": task_id, "action": prop["action"], "artifact_kind": artifact_kind, "by": actor.user_id,
                                "previous_health": a["health"], "previous_reason": a["health_reason"]}), asset_id])
        self.guard.audit(actor, ws, "asset.repair", asset_id, "started", {"task": task_id})
        return {"asset_id": asset_id, "action": prop["action"], "task_id": task_id, "status": "repairing"}

    def complete_repairs(self, ws: str) -> list[str]:
        """Finish repairs whose task has ended: a verified answer becomes the asset's next version; anything else puts the
        asset back to its earlier state with the reason. Safe to call often."""
        if self.runtime is None:
            return []
        done = []
        for r in self._db(ws).query("SELECT * FROM assets WHERE health = 'repairing'"):
            a = self._row(r)
            rep = a["repair"] or {}
            task = self.stores.control.query("SELECT * FROM tasks WHERE id = ?", [rep.get("task_id")])
            if not task or task[0]["status"] not in ("done", "failed", "cancelled"):
                continue
            t = dict(task[0])
            for k in ("request", "plan", "result"):
                t[k] = json.loads(t[k]) if isinstance(t.get(k), str) and t[k] else t.get(k)
            from .security import Principal as P  # noqa: PLC0415

            by = self.stores.control.query("SELECT id, email FROM users WHERE id = ?", [rep.get("by")])
            actor = P(by[0]["id"], by[0]["email"]) if by else P(a["owner"], "")
            validation = _validation_of(t.get("result"))
            if t["status"] == "done" and validation in ("verified", "verified_with_warnings"):
                if rep.get("artifact_kind") and self.artifacts is not None:
                    self.artifacts.build(actor, t, rep["artifact_kind"], asset_id=a["id"])
                elif a["kind"] == "workflow":
                    self._upsert(ws, actor, kind="workflow", ref_kind="workflow", ref_id=a["ref_id"], title=a["title"], task_id=t["id"],
                                 question=a["question"], depends=self._depends_from_result(ws, t["result"]) or a["depends"],
                                 validation=validation, sha256=None, note="re-verified", asset_id=a["id"])
                self.guard.audit(actor, ws, "asset.repair", a["id"], "ok", {"task": t["id"]})
            else:
                why = t.get("error") or f"the re-run was {validation.replace('_', ' ')}"
                self._db(ws).execute("UPDATE assets SET health = ?, health_reason = ?, repair = NULL WHERE id = ?",
                                     [rep.get("previous_health") or "stale", f"{rep.get('previous_reason') or ''}; repair did not verify: {why}"[:500], a["id"]])
                self.guard.audit(actor, ws, "asset.repair", a["id"], "failed", {"task": t["id"], "reason": str(why)[:200]})
            done.append(a["id"])
        return done

    def complete_all(self) -> int:
        """Worker tick: finish repairs in every workspace."""
        n = 0
        for w in self.stores.control.query("SELECT id FROM workspaces"):
            try:
                n += len(self.complete_repairs(w["id"]))
            except Exception:  # noqa: BLE001
                log.exception("completing repairs in %s failed", w["id"])
        return n

    # -- lifecycle ------------------------------------------------------------------------------

    def retire(self, actor: Principal, ws: str, asset_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "task.control", asset_id)
        a = self.get(actor, ws, asset_id)
        if a["kind"] == "dashboard" and self.studio is not None:
            self.studio.retire(actor, ws, a["ref_id"])
        self._db(ws).execute("UPDATE assets SET status = 'retired', updated_at = now() WHERE id = ?", [asset_id])
        self.guard.audit(actor, ws, "asset.retire", asset_id, "ok")
        return {"asset_id": asset_id, "status": "retired"}


__all__ = ["HEALTH", "KINDS", "REPORT_KINDS", "AssetService"]
