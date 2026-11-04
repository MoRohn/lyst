# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data classification and policy-aware execution.

Every dataset carries a classification level (public, internal, confidential, restricted) and optional tags
(personal, financial, regulated). Levels are set by administrators, with a reason, and every change is kept;
discovery may *suggest* a level from what the catalog measured (personal-data columns), never set one.

A workspace's data policy (versioned) says, per level, the lowest role that may read it, the highest level whose
derived results may be sent to an external model, and the highest level whose results may be exported as files.
The policy is enforced in the services, not in prompts:

* ``DataService.query`` calls ``check_read`` before a query runs: a dataset above the caller's role is refused
  with a plain explanation, and the refusal is audited;
* ``LLMClient.chat`` calls ``model_gate``: when a task has read data above the external-model ceiling, no external
  call is made and the analysis completes on Lyst's templates (the answer says so);
* ``ArtifactService.build`` calls ``check_export``: results computed from data above the export ceiling are not
  written to a file.

``evaluate`` answers ahead of time whether a task over given datasets would be allowed, narrowed or refused, and
why: members see the outcome and who to ask; administrators also see the rule that decided it.
"""

from __future__ import annotations

import json
from typing import Any

from .security import RANK, AccessDenied, Guard, Principal
from .store import Stores

LEVELS = ("public", "internal", "confidential", "restricted")
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
TAGS = ("personal", "financial", "regulated")
DEFAULT_POLICY: dict[str, Any] = {
    # the lowest role that may read data at each level
    "read_min_role": {"public": "viewer", "internal": "viewer", "confidential": "viewer", "restricted": "admin"},
    # results derived from data up to this level may go to an external model (definitions and aggregates only;
    # rows never leave). Restricted data never does by default.
    "external_model_max": "confidential",
    # results derived from data up to this level may be exported as files
    "export_max": "confidential",
}


def _level(value: str | None) -> str:
    return value if value in LEVEL_RANK else "internal"  # an unknown label is treated as internal, never as public


class PolicyDenied(AccessDenied):
    """A refusal by data policy. ``public`` is safe to show the caller; ``detail`` is for administrators."""

    def __init__(self, public: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(public)
        self.public = public
        self.detail = detail or {}


class GovernanceService:
    def __init__(self, stores: Stores, guard: Guard, *, external_model=lambda: False, runtime=None) -> None:
        self.stores = stores
        self.guard = guard
        self.external_model = external_model  # a callable: the installation may connect an external provider later
        self.runtime = runtime

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- classification -------------------------------------------------------------------------

    def classifications(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        db = self._db(ws)
        tags = {r["dataset"]: json.loads(r["tags"]) for r in db.query(
            "SELECT dataset, tags FROM classification_events e WHERE id = (SELECT max(id) FROM classification_events WHERE dataset = e.dataset)")}
        return [{"dataset": r["name"], "level": _level(r["classification"]), "label": r["classification"], "owner": r["owner"],
                 "tags": tags.get(r["name"], [])} for r in db.query("SELECT name, classification, owner FROM datasets ORDER BY name")]

    def classify(self, actor: Principal, ws: str, dataset: str, level: str, *, tags: list[str] | None = None, reason: str) -> dict[str, Any]:
        """Set a dataset's level and tags. Administrators only, with a reason; every change is kept."""
        self.guard.require(actor, ws, "workspace.admin", dataset)
        if level not in LEVEL_RANK:
            raise ValueError(f"level is one of {', '.join(LEVELS)}")
        bad = [t for t in tags or [] if t not in TAGS]
        if bad:
            raise ValueError(f"unknown tag(s) {', '.join(bad)}; tags are {', '.join(TAGS)}")
        if not reason or len(reason.strip()) < 3:
            raise ValueError("say why: a classification is a governance decision")
        db = self._db(ws)
        before = db.scalar("SELECT classification FROM datasets WHERE name = ?", [dataset])
        if before is None:
            raise KeyError(dataset)

        def tx(c):
            c.execute("UPDATE datasets SET classification = ?, updated_at = now() WHERE name = ?", [level, dataset])
            c.execute("INSERT INTO classification_events (id, dataset, level, previous, tags, reason, actor) "
                      "VALUES (nextval('classification_seq'), ?, ?, ?, ?, ?, ?)",
                      [dataset, level, before, json.dumps(sorted(set(tags or []))), reason.strip(), actor.user_id])

        db.write(tx)
        self.guard.audit(actor, ws, "data.classify", dataset, "ok", {"from": before, "to": level, "tags": tags or []})
        return {"dataset": dataset, "level": level, "previous": before, "tags": sorted(set(tags or []))}

    def classification_history(self, actor: Principal, ws: str, dataset: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read", dataset)
        rows = self._db(ws).query("SELECT level, previous, tags, reason, actor, created_at FROM classification_events "
                                  "WHERE dataset = ? ORDER BY id DESC", [dataset])
        for r in rows:
            r["tags"] = json.loads(r["tags"])
        return rows

    def suggest(self, actor: Principal, ws: str, insights_for) -> list[dict[str, Any]]:
        """Levels the catalog's own measurements argue for: personal-data columns suggest confidential with the
        personal tag. Suggestions only; nothing changes until an administrator classifies."""
        self.guard.require(actor, ws, "data.read")
        out = []
        for c in self.classifications(actor, ws):
            try:
                cols = insights_for(c["dataset"]).get("columns", [])
            except KeyError:
                continue
            personal = [x["name"] for x in cols if x.get("semantic_type") == "personal_data"]
            if personal and (LEVEL_RANK[c["level"]] < LEVEL_RANK["confidential"] or "personal" not in c["tags"]):
                out.append({"dataset": c["dataset"], "level": max(c["level"], "confidential", key=LEVEL_RANK.get),
                            "tags": sorted(set(c["tags"]) | {"personal"}),
                            "why": f"{', '.join(personal)} look{'s' if len(personal) == 1 else ''} like personal data"})
        return out

    # -- policy ---------------------------------------------------------------------------------

    def policy(self, actor: Principal | None, ws: str) -> dict[str, Any]:
        if actor is not None:
            self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT version, spec, created_by, created_at FROM data_policies ORDER BY version DESC LIMIT 1")
        if not rows:
            return {"version": 0, "spec": json.loads(json.dumps(DEFAULT_POLICY))}
        r = rows[0]
        r["spec"] = json.loads(r["spec"])
        return r

    def set_policy(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", "data-policy")
        spec = self.policy(actor, ws)["spec"]
        if changes.get("read_min_role"):
            spec["read_min_role"] = {**spec["read_min_role"], **changes["read_min_role"]}
        for k in ("external_model_max", "export_max"):
            if changes.get(k) is not None:
                spec[k] = changes[k]
        for lvl, role in spec["read_min_role"].items():
            if lvl not in LEVEL_RANK or role not in RANK:
                raise ValueError(f"read_min_role maps a level ({', '.join(LEVELS)}) to a role ({', '.join(RANK)})")
        ranks = [RANK[spec["read_min_role"][lvl]] for lvl in LEVELS]
        if ranks != sorted(ranks):
            raise ValueError("a more sensitive level cannot be readable by a lower role than a less sensitive one")
        for k in ("external_model_max", "export_max"):
            if spec[k] not in LEVEL_RANK and spec[k] != "none":
                raise ValueError(f"{k} is one of none, {', '.join(LEVELS)}")
        db = self._db(ws)
        version = (db.scalar("SELECT max(version) FROM data_policies") or 0) + 1
        db.execute("INSERT INTO data_policies (version, spec, created_by) VALUES (?, ?, ?)", [version, json.dumps(spec), actor.user_id])
        self.guard.audit(actor, ws, "data.policy", str(version), "ok", spec)
        return {"version": version, "spec": spec}

    @staticmethod
    def _within(level: str, ceiling: str) -> bool:
        return ceiling != "none" and LEVEL_RANK[_level(level)] <= LEVEL_RANK[ceiling]

    def _levels(self, ws: str, datasets: list[str] | set[str]) -> dict[str, dict[str, Any]]:
        if not datasets:
            return {}
        names = sorted(set(datasets))
        rows = self._db(ws).query(f"SELECT name, classification, owner FROM datasets WHERE name IN ({','.join('?' * len(names))})", names)
        return {r["name"]: {"level": _level(r["classification"]), "owner": r["owner"]} for r in rows}

    def _owner_name(self, user_id: str | None) -> str:
        if not user_id:
            return "the dataset owner"
        r = self.stores.control.query("SELECT name, email FROM users WHERE id = ?", [user_id])
        return f"{r[0]['name']} ({r[0]['email']})" if r else "the dataset owner"

    # -- enforcement points ---------------------------------------------------------------------

    def check_read(self, actor: Principal, ws: str, datasets: list[str]) -> None:
        """Called by DataService.query before a query runs."""
        if actor.kind == "system":
            return
        role = self.guard.role(actor, ws) or "viewer"
        spec = self.policy(None, ws)["spec"]
        for name, d in self._levels(ws, datasets).items():
            need = spec["read_min_role"].get(d["level"], "admin")
            if RANK[role] < RANK[need]:
                self.guard.audit(actor, ws, "policy.read", name, "denied", {"level": d["level"], "needs": need, "role": role})
                raise PolicyDenied(f"{name} is {d['level']} data and your role cannot read it. Ask {self._owner_name(d['owner'])} for access.",
                                   {"rule": "read_min_role", "level": d["level"], "needs": need, "role": role})

    def task_level(self, ws: str, task_id: str) -> tuple[str | None, list[str]]:
        """The most sensitive level a task has read so far, from the query log, and the datasets at that level."""
        rows = self._db(ws).query("SELECT DISTINCT unnest(CAST(datasets AS VARCHAR[])) AS d FROM query_history "
                                  "WHERE task_id = ? AND datasets IS NOT NULL AND outcome = 'ok'", [task_id])
        levels = self._levels(ws, [r["d"] for r in rows])
        if not levels:
            return None, []
        top = max((d["level"] for d in levels.values()), key=LEVEL_RANK.get)
        return top, sorted(n for n, d in levels.items() if d["level"] == top)

    def model_gate(self, ws: str, task_id: str | None) -> str | None:
        """Called by LLMClient.chat for an external model. A reason means: do not send."""
        if not task_id or not self.external_model():
            return None
        top, names = self.task_level(ws, task_id)
        if top is None:
            return None
        ceiling = self.policy(None, ws)["spec"]["external_model_max"]
        if self._within(top, ceiling):
            return None
        reason = f"data policy keeps results from {top} data ({', '.join(names)}) away from external models"
        if self.runtime is not None:
            self.runtime.event(task_id, "policy", f"No external model was used: {reason}.", {"level": top, "ceiling": ceiling})
        return reason

    def check_export(self, actor: Principal, ws: str, task_id: str) -> None:
        """Called by ArtifactService.build before a file is written."""
        top, names = self.task_level(ws, task_id)
        if top is None:
            return
        ceiling = self.policy(None, ws)["spec"]["export_max"]
        if not self._within(top, ceiling):
            self.guard.audit(actor, ws, "policy.export", task_id, "denied", {"level": top, "ceiling": ceiling})
            raise PolicyDenied(f"This result uses {top} data ({', '.join(names)}), which this workspace does not allow in exported files. "
                               "View it in Lyst, or ask an administrator.", {"rule": "export_max", "level": top, "ceiling": ceiling})

    # -- diagnostics ----------------------------------------------------------------------------

    def evaluate(self, actor: Principal, ws: str, *, datasets: list[str] | None = None, metrics: list[str] | None = None,
                 purpose: str = "analyze") -> dict[str, Any]:
        """Would this work be allowed, narrowed or refused for the caller, and why. Read-only.

        ``purpose`` is analyze, export or external_model. Members see the outcome and who to ask; administrators
        also see the rule and the levels involved."""
        role = self.guard.require(actor, ws, "workspace.read")
        is_admin = RANK[role] >= RANK["admin"]
        names = set(datasets or [])
        db = self._db(ws)
        for m in metrics or []:
            spec = db.scalar("SELECT v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name AND v.version = m.current_version "
                             "WHERE m.name = ?", [m])
            if spec is None:
                raise KeyError(m)
            names.add(json.loads(spec)["dataset"])
        spec = self.policy(None, ws)["spec"]
        levels = self._levels(ws, names)
        missing = sorted(names - set(levels))
        if missing:
            raise KeyError(missing[0])
        reasons, narrowed, refused = [], [], False
        for name, d in sorted(levels.items()):
            need = spec["read_min_role"].get(d["level"], "admin")
            if RANK[role] < RANK[need]:
                refused = True
                reasons.append({"dataset": name, "outcome": "refused",
                                "why": f"{name} is {d['level']} data; ask {self._owner_name(d['owner'])} for access.",
                                **({"rule": f"read_min_role[{d['level']}] = {need}, your role is {role}"} if is_admin else {})})
        top = max((d["level"] for d in levels.values()), key=LEVEL_RANK.get) if levels else None
        if top and not refused:
            if purpose == "export" and not self._within(top, spec["export_max"]):
                refused = True
                reasons.append({"outcome": "refused", "why": f"results from {top} data cannot be exported here; view them in Lyst.",
                                **({"rule": f"export_max = {spec['export_max']}"} if is_admin else {})})
            if purpose in ("analyze", "external_model") and self.external_model() and not self._within(top, spec["external_model_max"]):
                narrowed.append({"outcome": "narrowed", "why": f"results from {top} data stay on Lyst's own templates; no external model sees them.",
                                 **({"rule": f"external_model_max = {spec['external_model_max']}"} if is_admin else {})})
        # column and row policies narrow what is read without refusing it
        restricted = {r["dataset"] for r in db.query("SELECT DISTINCT dataset FROM column_policies")} & names
        for name in sorted(restricted):
            hidden = [r["column_name"] for r in db.query("SELECT column_name, min_role FROM column_policies WHERE dataset = ?", [name])
                      if RANK[role] < RANK[r["min_role"]]]
            if hidden:
                narrowed.append({"dataset": name, "outcome": "narrowed",
                                 "why": f"{len(hidden)} column{'s' if len(hidden) != 1 else ''} of {name} {'are' if len(hidden) != 1 else 'is'} hidden for your role.",
                                 **({"columns": hidden} if is_admin else {})})
        rows_limited = {r["dataset"] for r in db.query("SELECT DISTINCT dataset FROM row_policies")} & names
        for name in sorted(rows_limited):
            if actor.kind != "system" and db.scalar("SELECT count(*) FROM row_policies WHERE dataset = ? AND ((principal_kind = 'user' AND principal = ?) "
                                                    "OR (principal_kind = 'role' AND principal = ?))", [name, actor.user_id, role]):
                narrowed.append({"dataset": name, "outcome": "narrowed", "why": f"you see only some rows of {name}."})
        outcome = "refused" if refused else ("narrowed" if narrowed else "allowed")
        return {"outcome": outcome, "purpose": purpose, "datasets": sorted(names), "reasons": reasons + narrowed,
                **({"policy_version": self.policy(None, ws)["version"], "levels": {n: d["level"] for n, d in levels.items()}} if is_admin else {})}
