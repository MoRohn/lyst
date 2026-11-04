# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Routes for the Universal Analyst Core and the Specializations catalog (kept apart from the main route table).
No ``from __future__ import annotations`` here: FastAPI must see the ``User`` dependency type itself."""


from fastapi import FastAPI, HTTPException, Request

from ..specialists import browse, library, verification
from ..specialists.core import AnalystRequest


def register(app: FastAPI, L, User) -> None:  # noqa: N803 - the names match the main route table
    @app.get("/v1/specializations")
    def specializations(user: User, q: str | None = None, domain: str | None = None, min_level: int | None = None):
        return browse.search(q, domain, min_level)

    @app.get("/v1/specializations/overview")
    def specializations_overview(user: User):
        return browse.overview()

    @app.get("/v1/specializations/domains")
    def specialization_domains(user: User):
        return browse.domains()

    @app.get("/v1/specializations/library")
    def specialization_library(user: User):
        return {"capabilities": library.describe(), "verification_rules": verification.describe()}

    @app.get("/v1/specializations/{sid}")
    def specialization(sid: str, user: User):
        try:
            return browse.detail(sid)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/v1/workspaces/{ws}/specializations/{sid}")
    def specialization_for_workspace(request: Request, ws: str, sid: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.read")
        try:
            return browse.detail(sid, can=lambda perm: lyst.guard.can(user, ws, perm))
        except KeyError as e:
            raise HTTPException(404, str(e)) from None

    # The single entry point for analytical requests from any channel (text, voice, document, API, schedule).
    @app.post("/v1/workspaces/{ws}/analyst/requests")
    def analyst_request(request: Request, ws: str, body: AnalystRequest, user: User):
        if body.workspace_id != ws:
            raise HTTPException(400, "workspace_id in the body must match the path")
        return L(request).analyst_core.handle(user, body).model_dump()

    @app.get("/v1/tasks/{task_id}/expertise")
    def task_expertise(request: Request, task_id: str, user: User):
        return L(request).analyst_core.expertise_for_task(user, task_id)
