# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data quality routes: the flagged rows and cells of a dataset with Lyst's proposals, a person's review of them, Lyst
staging its fixes, and turning staged changes into a resolution that waits for an approval. What each does is in
``lyst.quality``. Registered from api/__init__.py."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


class ReviewIn(BaseModel):
    issue_ids: list[str] = Field(min_length=1, max_length=500)
    decision: str
    value: Any = None
    note: str | None = Field(default=None, max_length=1000)


class ProposeIn(BaseModel):
    issue_ids: list[str] | None = Field(default=None, max_length=500)


class ApplyIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/datasets/{name}/quality")
    def quality_report(request: Request, ws: str, name: str, user: User) -> dict[str, Any]:
        return L(request).quality.report(user, ws, name)

    @app.post("/v1/workspaces/{ws}/datasets/{name}/quality/review")
    def quality_review(request: Request, ws: str, name: str, body: ReviewIn, user: User) -> dict[str, Any]:
        return L(request).quality.review(user, ws, name, body.issue_ids, body.decision, value=body.value, note=body.note)

    @app.post("/v1/workspaces/{ws}/datasets/{name}/quality/propose")
    def quality_propose(request: Request, ws: str, name: str, body: ProposeIn, user: User) -> dict[str, Any]:
        return L(request).quality.propose(user, ws, name, body.issue_ids)

    @app.post("/v1/workspaces/{ws}/datasets/{name}/quality/apply")
    def quality_apply(request: Request, ws: str, name: str, body: ApplyIn, user: User) -> dict[str, Any]:
        return L(request).quality.apply(user, ws, name, note=body.note)

    @app.post("/v1/workspaces/{ws}/quality/resolutions/{rid}/undo")
    def quality_undo(request: Request, ws: str, rid: str, user: User) -> dict[str, Any]:
        return L(request).quality.undo(user, ws, rid)
