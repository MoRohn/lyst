# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Harness lane routes: a task's contract, its change receipts and its durable state; a workspace's classified
failures, the harness rules proposed from them, and the harness checklist. Registered from api/__init__.py."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel


class RuleEditIn(BaseModel):
    text: str


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/tasks/{task_id}/contract")
    def task_contract(request: Request, task_id: str, user: User) -> dict[str, Any]:
        return L(request).harness.contract(user, task_id)

    @app.get("/v1/tasks/{task_id}/receipts")
    def task_receipts(request: Request, task_id: str, user: User) -> list[dict[str, Any]]:
        return L(request).harness.receipts(user, task_id)

    @app.get("/v1/tasks/{task_id}/state")
    def task_state(request: Request, task_id: str, user: User) -> dict[str, Any]:
        return L(request).harness.state(user, task_id)

    @app.get("/v1/workspaces/{ws}/harness")
    def harness_report(request: Request, ws: str, user: User, days: int = 30) -> dict[str, Any]:
        return L(request).harness.report(user, ws, days=days)

    @app.get("/v1/workspaces/{ws}/harness/failures")
    def harness_failures(request: Request, ws: str, user: User, limit: int = 100) -> list[dict[str, Any]]:
        return L(request).harness.failures(user, ws, limit=limit)

    @app.get("/v1/workspaces/{ws}/harness/rules")
    def harness_rules(request: Request, ws: str, user: User, status: str | None = None) -> list[dict[str, Any]]:
        return L(request).harness.rules(user, ws, status=status)

    @app.put("/v1/workspaces/{ws}/harness/rules/{rule_id}")
    def harness_rule_edit(request: Request, ws: str, rule_id: str, body: RuleEditIn, user: User) -> dict[str, Any]:
        return L(request).harness.edit(user, ws, rule_id, body.text)

    @app.post("/v1/workspaces/{ws}/harness/rules/{rule_id}/retire")
    def harness_rule_retire(request: Request, ws: str, rule_id: str, user: User) -> dict[str, Any]:
        return L(request).harness.retire(user, ws, rule_id)
