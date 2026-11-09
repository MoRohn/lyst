# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Needs-you routes: the question Lyst asks about an answer that needs someone, with its choices, and carrying out the
one a person picks (or their own words). What each choice does is in ``lyst.needs``. Registered from api/__init__.py.

The paths sit under ``/needs`` so they never collide with ``POST /v1/tasks/{id}/{action}`` (pause, resume ...)."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field, model_validator


class ResolveIn(BaseModel):
    choice: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=500)
    confirm: bool = False

    @model_validator(mode="after")
    def _one(self) -> "ResolveIn":
        if (self.choice is None) == (self.text is None):
            raise ValueError("send a choice or your own words, not both")
        return self


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/tasks/{task_id}/needs")
    def task_needs(request: Request, task_id: str, user: User) -> dict[str, Any]:
        return L(request).needs.view(user, task_id)

    @app.post("/v1/tasks/{task_id}/needs/resolve")
    def resolve_needs(request: Request, task_id: str, body: ResolveIn, user: User) -> dict[str, Any]:
        return L(request).needs.resolve(user, task_id, choice=body.choice, text=body.text, confirm=body.confirm)
