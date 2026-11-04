# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipation routes: the activity center, one item with its trail, why it was surfaced, a person's response, and the
workspace's autonomy level and budgets. Registered from api/__init__.py in one block."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel


class AnticipationSettingsIn(BaseModel):
    level: int | str | None = None
    daily_prepare_limit: int | None = None
    daily_investigation_limit: int | None = None
    daily_compute_ms: int | None = None
    daily_surface_limit: int | None = None


class AnticipationActIn(BaseModel):
    action: str                    # an offered action id, or dismiss | snooze | useful | not_useful | incorrect
    until: datetime | None = None  # snooze
    note: str = ""


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/anticipation/settings")
    def anticipation_settings(request: Request, ws: str, user: User) -> dict[str, Any]:
        return L(request).anticipation.settings(user, ws)

    @app.patch("/v1/workspaces/{ws}/anticipation/settings")
    def anticipation_settings_set(request: Request, ws: str, body: AnticipationSettingsIn, user: User) -> dict[str, Any]:
        return L(request).anticipation.set_settings(user, ws, body.model_dump(exclude_none=True))

    @app.get("/v1/workspaces/{ws}/anticipations")
    def anticipations(request: Request, ws: str, user: User, status: str = "open", kind: str | None = None, limit: int = 50):
        return L(request).anticipation.list(user, ws, status=status, kind=kind, limit=min(limit, 200))

    @app.post("/v1/workspaces/{ws}/anticipations/refresh")
    def anticipations_refresh(request: Request, ws: str, user: User) -> dict[str, Any]:
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.admin", "anticipation")
        return lyst.anticipation.cycle(ws)

    @app.get("/v1/workspaces/{ws}/anticipations/{aid}")
    def anticipation(request: Request, ws: str, aid: str, user: User) -> dict[str, Any]:
        return L(request).anticipation.get(user, ws, aid)

    @app.get("/v1/workspaces/{ws}/anticipations/{aid}/why")
    def anticipation_why(request: Request, ws: str, aid: str, user: User) -> dict[str, Any]:
        return L(request).anticipation.explain(user, ws, aid)

    @app.post("/v1/workspaces/{ws}/anticipations/{aid}/act")
    def anticipation_act(request: Request, ws: str, aid: str, body: AnticipationActIn, user: User) -> dict[str, Any]:
        return L(request).anticipation.act(user, ws, aid, body.action, until=body.until, note=body.note)
