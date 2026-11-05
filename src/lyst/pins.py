# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Pins: the few things each person keeps one click away (an artifact, a dataset, a metric, a saved analysis).

Per person and per workspace, stored in the workspace database (``ui_pins``, migration 80), so a pin survives a
reload, another browser and a new session, and never crosses a workspace boundary. Pinning needs only read access
to the workspace; what a pin points at is still checked by its own route when it is opened.
"""

from __future__ import annotations

from typing import Any

from .security import Guard, Principal
from .store import Stores

KINDS = ("analysis", "chart", "dashboard", "workflow", "report", "briefing", "presentation", "workbook", "dataset",
         "metric")
LIMIT = 50


class PinService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores, self.guard = stores, guard

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        return self.stores.workspace(ws).query(
            "SELECT kind, ref_id AS id, title, created_at FROM ui_pins WHERE user_id = ? ORDER BY created_at DESC",
            [actor.user_id])

    def pin(self, actor: Principal, ws: str, kind: str, ref_id: str, title: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read")
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        if not ref_id or len(ref_id) > 200:
            raise ValueError("an id is required")
        db = self.stores.workspace(ws)
        if len(self.list(actor, ws)) >= LIMIT:
            raise ValueError(f"at most {LIMIT} pins; unpin something first")
        db.execute("INSERT OR REPLACE INTO ui_pins (user_id, kind, ref_id, title, created_at) VALUES (?, ?, ?, ?, now())",
                   [actor.user_id, kind, ref_id, (title or "")[:200]])
        return {"kind": kind, "id": ref_id, "title": title}

    def unpin(self, actor: Principal, ws: str, kind: str, ref_id: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read")
        self.stores.workspace(ws).execute("DELETE FROM ui_pins WHERE user_id = ? AND kind = ? AND ref_id = ?",
                                          [actor.user_id, kind, ref_id])
        return {"kind": kind, "id": ref_id, "pinned": False}
