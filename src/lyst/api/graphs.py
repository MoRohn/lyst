# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Graph lane routes: graph runs (plan, go, the loop's state, checks.md, graph.md), the harness and its learned rules,
the morning brief, the weekly review, and the Brain graph. Registered from api/__init__.py in one block."""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field


class IntakeIn(BaseModel):
    done: str | None = None
    proof: list[str] = Field(default_factory=list)
    never: str | None = None
    approval: str | None = None


class PlanIn(BaseModel):
    goal: str
    branches: list[str] | None = None
    title: str | None = None
    intake: IntakeIn | None = None
    max_attempts: int = 3
    budget_minutes: int = 120


class ReviseIn(BaseModel):
    job: str


class DecideIn(BaseModel):
    approve: bool = True


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/brain/graph")
    def brain_graph(request: Request, ws: str, user: User, runs: bool = True) -> dict[str, Any]:
        return L(request).graphs.brain(user, ws, runs=runs)

    @app.get("/v1/workspaces/{ws}/graph-runs")
    def graph_runs(request: Request, ws: str, user: User) -> list[dict[str, Any]]:
        return L(request).graphs.list(user, ws)

    @app.post("/v1/workspaces/{ws}/graph-runs")
    def graph_plan(request: Request, ws: str, body: PlanIn, user: User) -> dict[str, Any]:
        return L(request).graphs.plan(user, ws, body.goal, branches=body.branches, title=body.title,
                                      intake=body.intake.model_dump() if body.intake else None,
                                      max_attempts=body.max_attempts, budget_minutes=body.budget_minutes)

    @app.get("/v1/workspaces/{ws}/graph-runs/{run_id}")
    def graph_run(request: Request, ws: str, run_id: str, user: User) -> dict[str, Any]:
        return L(request).graphs.get(user, ws, run_id)

    @app.post("/v1/workspaces/{ws}/graph-runs/{run_id}/go")
    def graph_go(request: Request, ws: str, run_id: str, user: User) -> dict[str, Any]:
        return L(request).graphs.go(user, ws, run_id)

    @app.post("/v1/workspaces/{ws}/graph-runs/{run_id}/cancel")
    def graph_cancel(request: Request, ws: str, run_id: str, user: User) -> dict[str, Any]:
        return L(request).graphs.cancel(user, ws, run_id)

    @app.post("/v1/workspaces/{ws}/graph-runs/{run_id}/rerun")
    def graph_rerun(request: Request, ws: str, run_id: str, user: User) -> dict[str, Any]:
        return L(request).graphs.rerun(user, ws, run_id)

    @app.post("/v1/workspaces/{ws}/graph-runs/{run_id}/branches/{branch_id}/revise")
    def graph_revise(request: Request, ws: str, run_id: str, branch_id: str, body: ReviseIn, user: User) -> dict[str, Any]:
        return L(request).graphs.revise(user, ws, run_id, branch_id, body.job)

    @app.post("/v1/workspaces/{ws}/graph-runs/{run_id}/branches/{branch_id}/decide")
    def graph_decide(request: Request, ws: str, run_id: str, branch_id: str, body: DecideIn, user: User) -> dict[str, Any]:
        return L(request).graphs.approve(user, ws, run_id, branch_id, body.approve)

    @app.get("/v1/workspaces/{ws}/graph-runs/{run_id}/files")
    def graph_files(request: Request, ws: str, run_id: str, user: User) -> dict[str, str]:
        """The run's harness files: checks.md (one row per requirement with its evidence) and graph.md (Mermaid)."""
        g = L(request).graphs
        return {"checks.md": g.checks_md(user, ws, run_id), "graph.md": g.graph_md(user, ws, run_id)}

    @app.get("/v1/workspaces/{ws}/graph/harness")
    def graph_harness(request: Request, ws: str, user: User) -> dict[str, Any]:
        return L(request).graphs.harness(user, ws)

    @app.post("/v1/workspaces/{ws}/graph/rules/{rule_id}/retire")
    def graph_retire(request: Request, ws: str, rule_id: str, user: User) -> dict[str, Any]:
        return L(request).graphs.retire_rule(user, ws, rule_id)

    @app.get("/v1/workspaces/{ws}/graph/brief")
    def graph_brief(request: Request, ws: str, user: User, hours: int = 24) -> dict[str, Any]:
        return L(request).graphs.brief(user, ws, hours)

    @app.get("/v1/workspaces/{ws}/graph/review")
    def graph_review(request: Request, ws: str, user: User, days: int = 7) -> dict[str, Any]:
        return L(request).graphs.review(user, ws, days)
