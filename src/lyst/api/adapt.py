# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Routes for onboarding, domain packages, the business profile and the evidence behind answers (kept apart from the main route table so
parallel work on ``api/__init__.py`` does not collide). No ``from __future__ import annotations`` here: FastAPI must
see the ``User`` dependency type itself, which is a closure variable of ``create_app``)."""


from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from .. import domains


class AcceptIn(BaseModel):
    edits: dict[str, Any] | None = None


class ClassifyIn(BaseModel):
    level: str
    tags: list[str] = []
    reason: str


class DataPolicyIn(BaseModel):
    read_min_role: dict[str, str] | None = None
    external_model_max: str | None = None
    export_max: str | None = None


class PolicyCheckIn(BaseModel):
    datasets: list[str] = []
    metrics: list[str] = []
    purpose: str = "analyze"


class SpecDraftIn(BaseModel):
    request: str


class SpecIn(BaseModel):
    title: str
    body: dict[str, Any]


class SpecReviseIn(BaseModel):
    changes: dict[str, Any]
    note: str


class SpecLinkIn(BaseModel):
    kind: str
    id: str


class SpecMarkIn(BaseModel):
    status: str
    note: str = ""


class SpecNoteIn(BaseModel):
    note: str = ""


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

    # -- evidence and explanations -------------------------------------------------------------

    @app.get("/v1/tasks/{task_id}/evidence")
    def task_evidence(request: Request, task_id: str, user: User):
        return L(request).evidence.graph(user, task_id)

    # q=reproduce re-runs the recorded queries (POST /v1/tasks/{id}/{action} is the task-control route)
    @app.get("/v1/tasks/{task_id}/explain")
    def task_explain(request: Request, task_id: str, user: User, q: str = "how", figure: str | None = None):
        return L(request).evidence.explain(user, task_id, q, figure=figure)

    @app.get("/v1/artifacts/{artifact_id}/evidence")
    def artifact_evidence(request: Request, artifact_id: str, user: User):
        return L(request).evidence.for_artifact(user, artifact_id)

    # -- classification and data policy ---------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/classifications")
    def classifications(request: Request, ws: str, user: User):
        return L(request).governance.classifications(user, ws)

    @app.get("/v1/workspaces/{ws}/classifications/suggestions")
    def classification_suggestions(request: Request, ws: str, user: User):
        lyst = L(request)
        return lyst.governance.suggest(user, ws, lambda name: lyst.catalog.insights(user, ws, name))

    @app.put("/v1/workspaces/{ws}/datasets/{name}/classification")
    def classify_dataset(request: Request, ws: str, name: str, body: ClassifyIn, user: User):
        return L(request).governance.classify(user, ws, name, body.level, tags=body.tags, reason=body.reason)

    @app.get("/v1/workspaces/{ws}/datasets/{name}/classification")
    def classification_history(request: Request, ws: str, name: str, user: User):
        return L(request).governance.classification_history(user, ws, name)

    @app.get("/v1/workspaces/{ws}/data-policy")
    def data_policy(request: Request, ws: str, user: User):
        return L(request).governance.policy(user, ws)

    @app.put("/v1/workspaces/{ws}/data-policy")
    def set_data_policy(request: Request, ws: str, body: DataPolicyIn, user: User):
        return L(request).governance.set_policy(user, ws, body.model_dump())

    @app.post("/v1/workspaces/{ws}/policy/check")
    def policy_check(request: Request, ws: str, body: PolicyCheckIn, user: User):
        return L(request).governance.evaluate(user, ws, datasets=body.datasets, metrics=body.metrics, purpose=body.purpose)

    # -- where things stand and what approvals would do ------------------------------------------

    @app.get("/v1/workspaces/{ws}/standing")
    def standing(request: Request, ws: str, user: User, days: int = 7):
        return L(request).standing.standing(user, ws, days=min(max(days, 1), 90))

    @app.get("/v1/workspaces/{ws}/approvals/explained")
    def approvals_explained(request: Request, ws: str, user: User):
        return L(request).standing.approvals(user, ws)

    # -- cost and value --------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/costs")
    def costs(request: Request, ws: str, user: User, days: int = 30, by: str = "user"):
        return L(request).costs.costs(user, ws, days=min(max(days, 1), 365), by=by)

    @app.get("/v1/workspaces/{ws}/value")
    def value(request: Request, ws: str, user: User, days: int = 30, manual_minutes_per_answer: float | None = None):
        return L(request).costs.value(user, ws, days=min(max(days, 1), 365), manual_minutes_per_answer=manual_minutes_per_answer)

    @app.get("/v1/workspaces/{ws}/costs/recommendations")
    def cost_recommendations(request: Request, ws: str, user: User, days: int = 30):
        return L(request).costs.recommendations(user, ws, days=min(max(days, 1), 365))

    # -- project specifications ------------------------------------------------------------------

    @app.post("/v1/workspaces/{ws}/specs/draft")
    def spec_draft(request: Request, ws: str, body: SpecDraftIn, user: User):
        return L(request).specs.draft(user, ws, body.request)

    @app.post("/v1/workspaces/{ws}/specs")
    def spec_create(request: Request, ws: str, body: SpecIn, user: User):
        return L(request).specs.create(user, ws, body.title, body.body)

    @app.get("/v1/workspaces/{ws}/specs")
    def spec_list(request: Request, ws: str, user: User):
        return L(request).specs.list(user, ws)

    @app.get("/v1/workspaces/{ws}/specs/{sid}")
    def spec_get(request: Request, ws: str, sid: str, user: User, history: bool = False):
        s = L(request).specs
        return s.history(user, ws, sid) if history else s.get(user, ws, sid)

    @app.post("/v1/workspaces/{ws}/specs/{sid}/revise")
    def spec_revise(request: Request, ws: str, sid: str, body: SpecReviseIn, user: User):
        return L(request).specs.revise(user, ws, sid, body.changes, note=body.note)

    @app.post("/v1/workspaces/{ws}/specs/{sid}/requirements/{rid}/link")
    def spec_link(request: Request, ws: str, sid: str, rid: str, body: SpecLinkIn, user: User):
        return L(request).specs.link(user, ws, sid, rid, body.kind, body.id)

    @app.post("/v1/workspaces/{ws}/specs/{sid}/requirements/{rid}/mark")
    def spec_mark(request: Request, ws: str, sid: str, rid: str, body: SpecMarkIn, user: User):
        return L(request).specs.mark(user, ws, sid, rid, status=body.status, note=body.note)

    @app.post("/v1/workspaces/{ws}/specs/{sid}/complete")
    def spec_complete(request: Request, ws: str, sid: str, user: User, body: SpecNoteIn | None = None):
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        from ..specs import SpecIncomplete  # noqa: PLC0415

        try:
            return L(request).specs.complete(user, ws, sid, note=body.note if body else "")
        except SpecIncomplete as exc:
            return JSONResponse({"detail": str(exc), "unmet": exc.unmet}, status_code=409)

    @app.post("/v1/workspaces/{ws}/specs/{sid}/cancel")
    def spec_cancel(request: Request, ws: str, sid: str, user: User, body: SpecNoteIn | None = None):
        return L(request).specs.cancel(user, ws, sid, note=body.note if body else "")

    @app.get("/v1/workspaces/{ws}/profile")
    def business_profile(request: Request, ws: str, user: User, history: bool = False):
        o = L(request).onboarding
        return o.profile_history(user, ws) if history else o.profile(user, ws)

    @app.put("/v1/workspaces/{ws}/profile")
    def set_business_profile(request: Request, ws: str, body: ProfileIn, user: User):
        return L(request).onboarding.set_profile(user, ws, body.model_dump())
