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
import re
import uuid
from typing import Any

from .security import Guard, Principal
from .store import Stores

LAYERS = ("task", "user", "team", "organization", "procedural", "episodic")
TRUST = ("observed", "user_validated", "approved")


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

    def promote(self, actor: Principal, workspace_id: str, memory_id: str) -> None:
        self.guard.require(actor, workspace_id, "memory.promote", memory_id)
        self.stores.workspace(workspace_id).execute(
            "UPDATE memory SET trust = 'approved', layer = 'organization' WHERE id = ? AND revoked_at IS NULL",
            [memory_id],
        )
        self.guard.audit(actor, workspace_id, "memory.promote", memory_id, "ok")

    def revoke(self, actor: Principal, workspace_id: str, memory_id: str) -> None:
        self.guard.require(actor, workspace_id, "memory.write", memory_id)
        self.stores.workspace(workspace_id).execute(
            "UPDATE memory SET revoked_at = now() WHERE id = ?", [memory_id])
        self.guard.audit(actor, workspace_id, "memory.revoke", memory_id, "ok")

    def recall(self, actor: Principal, workspace_id: str, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """Keyword recall over entries the caller may see: their own user layer and every
        shared layer. Approved organizational entries rank first."""
        self.guard.require(actor, workspace_id, "workspace.read")
        words = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}
        rows = self.stores.workspace(workspace_id).query(
            "SELECT id, layer, scope, owner, content, provenance, trust, occurred_at FROM ("
            "SELECT *, created_at AS occurred_at FROM memory) WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) AND (layer <> 'user' OR scope = ?)",
            [actor.user_id],
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
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = []
        for _, _, r in scored[:limit]:
            r["provenance"] = json.loads(r["provenance"])
            out.append(r)
        return out

    def list(self, actor: Principal, workspace_id: str, layer: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "workspace.read")
        sql = ("SELECT id, layer, scope, owner, content, trust, created_at FROM memory WHERE revoked_at IS NULL "
               "AND (layer <> 'user' OR scope = ?)")
        params: list[Any] = [actor.user_id]
        if layer:
            sql += " AND layer = ?"
            params.append(layer)
        return self.stores.workspace(workspace_id).query(sql + " ORDER BY created_at DESC", params)
