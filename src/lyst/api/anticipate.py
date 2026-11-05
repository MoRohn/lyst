# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipation routes: the activity center, one item with its trail, why it was surfaced, a person's response, and the
workspace's autonomy level and budgets. Registered from api/__init__.py in one block."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel


class AnticipationSettingsIn(BaseModel):
    level: int | str | None = None
    daily_prepare_limit: int | None = None
    daily_investigation_limit: int | None = None
    daily_compute_ms: int | None = None
    daily_surface_limit: int | None = None


class AttentionIn(BaseModel):
    opted_out: bool | None = None
    min_score: float | None = None
    max_per_day: int | None = None
    quiet_start: str | None = None   # HH:MM in the person's time zone; "" clears
    quiet_end: str | None = None
    timezone: str | None = None      # IANA name
    muted_kinds: list[str] | None = None


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

    @app.get("/v1/workspaces/{ws}/anticipation/quality")
    def anticipation_quality(request: Request, ws: str, user: User, days: int = 30) -> dict[str, Any]:
        return L(request).anticipation.quality(user, ws, days=days)

    @app.get("/v1/workspaces/{ws}/attention")
    def attention(request: Request, ws: str, user: User) -> dict[str, Any]:
        """The caller's own attention preferences for anticipation."""
        return L(request).anticipation.attention(user, ws)

    @app.put("/v1/workspaces/{ws}/attention")
    def attention_set(request: Request, ws: str, body: AttentionIn, user: User) -> dict[str, Any]:
        return L(request).anticipation.set_attention(user, ws, body.model_dump(exclude_unset=True))

    @app.get("/v1/workspaces/{ws}/anticipations")
    def anticipations(request: Request, ws: str, user: User, status: str = "open", kind: str | None = None,
                      limit: Annotated[int, Query(ge=1, le=200)] = 50,
                      subject: str | None = None):
        """The activity center; ``subject`` narrows it to one object (a task id for the next steps under an answer)."""
        return L(request).anticipation.list(user, ws, status=status, kind=kind, limit=limit, subject_id=subject)

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
