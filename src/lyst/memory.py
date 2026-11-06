# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Hierarchical memory with provenance, trust and revocation.

Layers: working (lives in a task's checkpoints), task, user, team, organization, procedural,
episodic. Only ``organization`` entries with trust ``approved`` are authoritative. An
observation made by an agent is stored as ``observed`` and needs an admin to promote it, so
untrusted content never becomes canonical knowledge by itself.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .security import Guard, Principal
from .store import Stores

LAYERS = ("task", "user", "team", "organization", "procedural", "episodic")


def stale_days() -> int:
    """Unapproved memory older than this is flagged as possibly out of date (approved knowledge is not aged)."""
    return int(os.environ.get("LYST_MEMORY_STALE_DAYS", "365"))
TRUST = ("observed", "user_validated", "approved")


def retention_days() -> int | None:
    """Agent-observed episodic and task memory older than this is deleted by the daily prune; approved and
    person-validated memory never is. ``LYST_RETENTION_DAYS_MEMORY``, default 365 (decided 2026-11-06); 0 keeps all."""
    return int(os.environ.get("LYST_RETENTION_DAYS_MEMORY", "365")) or None


def recall_scan() -> int:
    """At most this many memory entries are scored per recall: approved first, then newest. Bounds question latency
    however much episodic memory a workspace collects."""
    return max(100, int(os.environ.get("LYST_MEMORY_RECALL_SCAN", "5000")))


class MemoryService:
    def __init__(self, stores: Stores, guard: Guard, llm=None) -> None:
        self.stores = stores
        self.guard = guard
        self.llm = llm

    def remember(self, actor: Principal, workspace_id: str, layer: str, content: str, *,
                 provenance: dict[str, Any], trust: str = "observed", scope: str | None = None) -> str:
        if layer not in LAYERS or trust not in TRUST:
            raise ValueError("unknown layer or trust level")
        self.guard.require(actor, workspace_id, "memory.write")
        if trust == "approved" or layer == "organization":
            self.guard.require(actor, workspace_id, "memory.promote")
        scope = scope or (actor.user_id if layer == "user" else workspace_id)
        # the same fact at the same layer and scope is kept once (and embedded once), not once per repeat
        same = self.stores.workspace(workspace_id).scalar(
            "SELECT id FROM memory WHERE layer = ? AND scope = ? AND content = ? AND trust = ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1", [layer, scope, content, trust])
        if same:
            return same
        mid = "m_" + uuid.uuid4().hex[:12]
        self.stores.workspace(workspace_id).execute(
            "INSERT INTO memory (id, layer, scope, owner, content, provenance, trust) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [mid, layer, scope, actor.user_id, content, json.dumps(provenance, default=str), trust],
        )
        if self.llm and self.llm.can_embed(workspace_id):
            try:
                v = self.llm.embed(workspace_id, [content], purpose="embed_memory")[0]
                self.stores.workspace(workspace_id).execute(
                    "INSERT OR REPLACE INTO memory_vectors (memory_id, model, vector) VALUES (?, ?, ?)", [mid, self.llm.embed_model, v])
            except Exception as exc:  # noqa: BLE001 - keyword recall still works
                import logging  # noqa: PLC0415

                logging.getLogger("lyst.memory").warning("memory embedding failed: %s", exc)
        self.guard.audit(actor, workspace_id, "memory.write", mid, "ok", {"layer": layer, "trust": trust})
        return mid

    def _entry(self, workspace_id: str, memory_id: str) -> dict[str, Any]:
        rows = self.stores.workspace(workspace_id).query(
            "SELECT id, layer, scope, owner, trust, revoked_at FROM memory WHERE id = ?", [memory_id])
        if not rows or rows[0]["revoked_at"] is not None:
            raise KeyError(f"no memory {memory_id}")
        return rows[0]

    def promote(self, actor: Principal, workspace_id: str, memory_id: str) -> None:
        self.guard.require(actor, workspace_id, "memory.promote", memory_id)
        m = self._entry(workspace_id, memory_id)
        if m["layer"] == "user" and m["scope"] != actor.user_id:
            # someone's private memory becomes organizational knowledge only by their own hand
            raise PermissionError("only its owner can share a private memory")
        self.stores.workspace(workspace_id).execute(
            "UPDATE memory SET trust = 'approved', layer = 'organization' WHERE id = ? AND revoked_at IS NULL",
            [memory_id],
        )
        self.guard.audit(actor, workspace_id, "memory.promote", memory_id, "ok")

    def revoke(self, actor: Principal, workspace_id: str, memory_id: str) -> None:
        self.guard.require(actor, workspace_id, "memory.write", memory_id)
        m = self._entry(workspace_id, memory_id)
        if m["layer"] == "user" and m["scope"] != actor.user_id:
            raise PermissionError("a private memory can only be removed by its owner")
        if m["trust"] == "approved" or m["layer"] == "organization":
            # what only an admin may approve, only an admin may take back
            self.guard.require(actor, workspace_id, "memory.promote", memory_id)
        self.stores.workspace(workspace_id).execute(
            "UPDATE memory SET revoked_at = now() WHERE id = ?", [memory_id])
        self.guard.audit(actor, workspace_id, "memory.revoke", memory_id, "ok")

    def recall(self, actor: Principal, workspace_id: str, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """Keyword recall over entries the caller may see: their own user layer and every
        shared layer. Approved organizational entries rank first."""
        self.guard.require(actor, workspace_id, "workspace.read")
        words = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}
        # a bounded candidate set: approved knowledge first, then validated, then the newest observations
        rows = self.stores.workspace(workspace_id).query(
            "SELECT id, layer, scope, owner, content, provenance, trust, occurred_at FROM ("
            "SELECT *, created_at AS occurred_at FROM memory) WHERE revoked_at IS NULL AND disputed_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) AND (layer <> 'user' OR scope = ?) "
            "ORDER BY CASE trust WHEN 'approved' THEN 0 WHEN 'user_validated' THEN 1 ELSE 2 END, created_at DESC LIMIT ?",
            [actor.user_id, recall_scan()],
        )
        scored = []
        rank = {"approved": 2, "user_validated": 1, "observed": 0}
        sims: dict[str, float] = {}
        if self.llm and self.llm.can_embed(workspace_id):
            try:
                qv = self.llm.embed(workspace_id, [text], purpose="recall")[0]
                sims = {r["memory_id"]: float(r["sim"]) for r in self.stores.workspace(workspace_id).query(
                    "SELECT memory_id, list_cosine_similarity(vector, ?::FLOAT[]) AS sim FROM memory_vectors WHERE model = ?",
                    [qv, self.llm.embed_model])}
            except Exception:  # noqa: BLE001
                sims = {}
        for r in rows:
            overlap = len(words & set(re.findall(r"[a-z0-9]{3,}", r["content"].lower())))
            sim = sims.get(r["id"], 0.0)
            if overlap or sim >= 0.75:
                scored.append((rank[r["trust"]], overlap + 3 * sim, r))
        for r in rows:
            r["provenance"] = json.loads(r["provenance"]) if isinstance(r["provenance"], str) else r["provenance"]
        checked = self.applicability(workspace_id, [s[2] for s in scored])
        # entries that still apply first; within that, trust and relevance as before
        scored.sort(key=lambda x: (checked[x[2]["id"]]["applicable"], x[0], x[1]), reverse=True)
        out = []
        for _, _, r in scored[:limit]:
            out.append({**r, **checked[r["id"]]})
        return out

    def applicability(self, workspace_id: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Whether each memory still applies: not when the data it was learned from has a newer version, or the
        metric definition it used has changed, or (unless approved) it is older than ``LYST_MEMORY_STALE_DAYS``.
        History is not permanently valid; this says when a past conclusion may no longer hold, and why."""
        db = self.stores.workspace(workspace_id)
        current = {r["name"]: r["current_version"] for r in db.query("SELECT name, current_version FROM datasets")}
        metric_now = {r["name"]: r["current_version"] for r in db.query("SELECT name, current_version FROM metrics")}
        out: dict[str, dict[str, Any]] = {}
        now = datetime.now(UTC)
        provs = {r["id"]: (r["provenance"] if isinstance(r["provenance"], dict) else json.loads(r["provenance"] or "{}"))
                 for r in rows}
        aids = sorted({p["analysis_id"] for p in provs.values() if p.get("analysis_id")})
        evidence: dict[str, Any] = {}
        if aids:  # one query for every analysis the memories came from, not one per memory
            evidence = {a["id"]: a["evidence"] for a in db.query(
                f"SELECT id, evidence FROM analyses WHERE id IN ({', '.join('?' * len(aids))})", aids)}
        for r in rows:
            prov = provs[r["id"]]
            sources, metric = list(prov.get("sources") or []), prov.get("metric")
            if prov.get("analysis_id"):
                if prov["analysis_id"] in evidence:
                    ev = json.loads(evidence[prov["analysis_id"]])
                    sources += ev.get("sources", [])
                    m = (ev.get("metric") or {}).get("spec", {}).get("name")
                    if m and ev.get("metric_version"):
                        metric = {"name": m, "version": ev["metric_version"]}
            reasons = []
            seen = set()
            for src in sources:
                ds, ver = src.get("dataset"), src.get("version")
                if not ds or (ds, ver) in seen:
                    continue
                seen.add((ds, ver))
                if ds not in current:
                    reasons.append(f"it was learned from {ds}, which no longer exists")
                elif ver is not None and current[ds] != ver:
                    reasons.append(f"it was learned from {ds} v{ver}; the data is now v{current[ds]}")
            if metric and metric.get("name") in metric_now and metric.get("version") and metric_now[metric["name"]] != metric["version"]:
                reasons.append(f"the definition of {metric['name']} changed (v{metric['version']} → v{metric_now[metric['name']]})")
            created = r.get("created_at") or r.get("occurred_at")
            if r.get("trust") != "approved" and created is not None:
                age = (now - (created if created.tzinfo else created.replace(tzinfo=UTC))).days
                if age > stale_days():
                    reasons.append(f"it is {age} days old")
            out[r["id"]] = {"applicable": not reasons, "stale_reason": "; ".join(reasons) or None}
        return out

    def review(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        """Memories that may no longer apply, with the reason, for an admin to revoke or re-confirm."""
        self.guard.require(actor, workspace_id, "memory.promote")
        rows = self.stores.workspace(workspace_id).query(
            "SELECT id, layer, scope, owner, content, provenance, trust, created_at FROM memory WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC")
        checked = self.applicability(workspace_id, rows)
        return [{**{k: v for k, v in r.items() if k != "provenance"}, **checked[r["id"]]}
                for r in rows if not checked[r["id"]]["applicable"]]

    def list(self, actor: Principal, workspace_id: str, layer: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "workspace.read")
        sql = ("SELECT id, layer, scope, owner, content, trust, created_at, disputed_at FROM memory WHERE revoked_at IS NULL "
               "AND (expires_at IS NULL OR expires_at > now()) AND (layer <> 'user' OR scope = ?)")
        params: list[Any] = [actor.user_id]
        if layer:
            sql += " AND layer = ?"
            params.append(layer)
        return self.stores.workspace(workspace_id).query(sql + " ORDER BY created_at DESC", params)
