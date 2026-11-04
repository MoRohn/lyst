# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Routes for onboarding, domain packages and the business profile (kept apart from the main route table so
parallel work on ``api/__init__.py`` does not collide). No ``from __future__ import annotations`` here: FastAPI must
see the ``User`` dependency type itself, which is a closure variable of ``create_app``)."""


from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from .. import domains


class AcceptIn(BaseModel):
    edits: dict[str, Any] | None = None


class ProfileIn(BaseModel):
    domains: list[str] | None = None
    terminology: dict[str, Any] | None = None
    fiscal_year_start_month: int | None = None
    reporting_period: str | None = None
    objectives: list[str] | None = None


def register(app: FastAPI, L, User) -> None:  # noqa: N803 - the names match the main route table
    @app.get("/v1/domains")
    def domain_packages(user: User):
        return [{"id": p.id, "version": p.version, "title": p.title, "summary": p.summary, "metrics": [m.name for m in p.metrics],
                 "questions": p.questions} for p in domains.packages().values()]

    @app.get("/v1/domains/{package_id}")
    def domain_package(package_id: str, user: User):
        return domains.get(package_id).model_dump()

    @app.get("/v1/workspaces/{ws}/onboarding")
    def onboarding_status(request: Request, ws: str, user: User):
        return L(request).onboarding.status(user, ws)

    @app.post("/v1/workspaces/{ws}/onboarding/discover")
    def onboarding_discover(request: Request, ws: str, user: User):
        return L(request).onboarding.discover(user, ws)

    @app.get("/v1/workspaces/{ws}/onboarding/proposals")
    def onboarding_proposals(request: Request, ws: str, user: User, status: str | None = "proposed", kind: str | None = None):
        return L(request).onboarding.proposals(user, ws, status=None if status == "all" else status, kind=kind)

    @app.post("/v1/workspaces/{ws}/onboarding/proposals/{pid}/accept")
    def onboarding_accept(request: Request, ws: str, pid: str, user: User, body: AcceptIn | None = None):
        return L(request).onboarding.accept(user, ws, pid, (body.edits if body else None))

    @app.post("/v1/workspaces/{ws}/onboarding/proposals/{pid}/dismiss")
    def onboarding_dismiss(request: Request, ws: str, pid: str, user: User):
        return L(request).onboarding.dismiss(user, ws, pid)

    @app.get("/v1/workspaces/{ws}/profile")
    def business_profile(request: Request, ws: str, user: User, history: bool = False):
        o = L(request).onboarding
        return o.profile_history(user, ws) if history else o.profile(user, ws)

    @app.put("/v1/workspaces/{ws}/profile")
    def set_business_profile(request: Request, ws: str, body: ProfileIn, user: User):
        return L(request).onboarding.set_profile(user, ws, body.model_dump())
