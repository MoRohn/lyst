# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""UI lane routes: each person's pins. Registered from api/__init__.py in one block."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel


class PinIn(BaseModel):
    title: str | None = None


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/pins")
    def pins(request: Request, ws: str, user: User) -> list[dict[str, Any]]:
        return L(request).pins.list(user, ws)

    @app.put("/v1/workspaces/{ws}/pins/{kind}/{ref_id}")
    def pin(request: Request, ws: str, kind: str, ref_id: str, body: PinIn, user: User) -> dict[str, Any]:
        return L(request).pins.pin(user, ws, kind, ref_id, body.title)

    @app.delete("/v1/workspaces/{ws}/pins/{kind}/{ref_id}")
    def unpin(request: Request, ws: str, kind: str, ref_id: str, user: User) -> dict[str, Any]:
        return L(request).pins.unpin(user, ws, kind, ref_id)
