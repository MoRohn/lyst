# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Learning lane routes: a person's verdict on an answer (correct, incorrect, partly), withdrawing it, and the verdicts
on an analysis, and the learning report. What a verdict changes is in ``lyst.feedback`` and docs/LEARNING.md. Registered from api/__init__.py."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


class FeedbackIn(BaseModel):
    verdict: str
    note: str | None = Field(default=None, max_length=2000)
    correction: str | None = Field(default=None, max_length=4000)


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.post("/v1/workspaces/{ws}/analyses/{analysis_id}/feedback")
    def give_feedback(request: Request, ws: str, analysis_id: str, body: FeedbackIn, user: User) -> dict[str, Any]:
        return L(request).feedback.give(user, ws, analysis_id, body.verdict, note=body.note, correction=body.correction)

    @app.get("/v1/workspaces/{ws}/analyses/{analysis_id}/feedback")
    def analysis_feedback(request: Request, ws: str, analysis_id: str, user: User) -> dict[str, Any]:
        return L(request).feedback.for_analysis(user, ws, analysis_id)

    @app.post("/v1/workspaces/{ws}/feedback/{feedback_id}/withdraw")
    def withdraw_feedback(request: Request, ws: str, feedback_id: str, user: User) -> dict[str, Any]:
        return L(request).feedback.withdraw(user, ws, feedback_id)

    @app.get("/v1/workspaces/{ws}/feedback/summary")
    def feedback_summary(request: Request, ws: str, user: User, days: int = 90) -> dict[str, Any]:
        return L(request).feedback.summary(user, ws, days=days)

    @app.get("/v1/workspaces/{ws}/learning")
    def learning_report(request: Request, ws: str, user: User, days: int = 90) -> dict[str, Any]:
        return L(request).learning.report(user, ws, days=days)
