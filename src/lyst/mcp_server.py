# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst as an MCP server, so other agents and tools can delegate analytical work to it.

Mounted at ``/mcp`` on the API (streamable HTTP, stateless, JSON responses). Every tool call carries
the caller's Lyst bearer token in the ``Authorization`` header and runs under that person's roles:
the same guard, the same audit log, the same workspace isolation as the UI. There is no separate MCP
identity and nothing a tool can do that the API cannot.

Tools: ``ask`` (a question through the analyst, waits for the answer), ``workspaces``, ``datasets``,
``metrics``, ``query`` (guarded read-only SQL), ``findings`` (proactive monitor results), ``task``.
"""

from __future__ import annotations

import functools
import json
import time
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .app import Lyst
from .connectors import ConnectorError
from .data.sqlguard import QueryRejected
from .security import AccessDenied, Principal


def _principal(lyst: Lyst, ctx: Context) -> Principal:
    headers: Mapping[str, str] | None = ctx.headers
    auth = (headers or {}).get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    return lyst.guard.authenticate(token)


def _guarded(fn):
    """Lyst's own refusals reach the caller as plain text; anything else stays generic."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AccessDenied, QueryRejected, ConnectorError, KeyError, ValueError) as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def build_mcp(lyst: Lyst) -> MCPServer:
    mcp = MCPServer(
        "lyst", title="Lyst", version="0.1.0",
        instructions="Lyst is a governed AI analyst. Ask it business questions about a workspace's approved metrics; "
                     "every number comes from an executed query and the answer carries its evidence. Pass the user's Lyst "
                     "bearer token as the Authorization header. Start with `workspaces`, then `metrics` to see what is "
                     "approved, then `ask`.")

    @mcp.tool(description="List the workspaces the caller belongs to, with their role in each.")
    @_guarded
    def workspaces(ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return lyst.guard.workspaces_for(user)

    @mcp.tool(description="Governed datasets in a workspace: name, title, version, rows, columns.")
    @_guarded
    def datasets(workspace: str, ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [{k: d[k] for k in ("name", "title", "description", "version", "row_count", "schema")} for d in lyst.data.catalog(user, workspace)]

    @mcp.tool(description="Approved metric definitions in a workspace, the only numbers Lyst will compute for a business question.")
    @_guarded
    def metrics(workspace: str, ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [{"name": m.name, "title": m.title, "description": m.description, "dataset": m.dataset, "kind": m.kind,
                 "dimensions": m.dimensions, "format": m.format, "synonyms": m.synonyms} for m in lyst.semantic.metrics(user, workspace)]

    @mcp.tool(description="Search the workspace's datasets, columns, metrics, glossary, saved analyses, dashboards and documents "
                          "for a business question; each hit names its directly connected assets.")
    @_guarded
    def search(workspace: str, query: str, ctx: Context, limit: int = 20) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        return json.loads(json.dumps(lyst.catalog.search(user, workspace, query, limit=min(limit, 100)), default=str))

    @mcp.tool(description="Where a metric comes from: definition, compiled SQL, source dataset and origin, contract status, "
                          "quality notes, version history, and the dashboards, workflows and monitors that depend on it.")
    @_guarded
    def metric_lineage(workspace: str, metric: str, ctx: Context) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        return json.loads(json.dumps(lyst.catalog.explain_metric(user, workspace, metric), default=str))

    @mcp.tool(description="Ask the analyst a question or give it an assignment. Waits up to timeout_seconds for the verified answer "
                          "and returns it with evidence, checks and the queries it ran; returns the task id if still running.")
    @_guarded
    def ask(workspace: str, question: str, ctx: Context, timeout_seconds: int = 60) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        tid = lyst.runtime.submit(user, workspace, question)
        deadline = time.monotonic() + max(1, min(timeout_seconds, 600))
        while time.monotonic() < deadline:
            if not lyst.runtime._threads:  # no background workers (tests, stdio): drive it here
                lyst.runtime.drain()
            t = lyst.runtime.get(user, tid)
            if t["status"] in ("done", "failed", "cancelled", "awaiting_approval"):
                return {"task_id": tid, "status": t["status"], "error": t.get("error"), "result": t.get("result")}
            time.sleep(0.2)
        return {"task_id": tid, "status": "running", "result": None}

    @mcp.tool(description="Run one read-only SQL statement against the workspace's governed datasets (by dataset name). "
                          "The same guard applies as in the UI: no writes, no files, column policies by role.")
    @_guarded
    def query(workspace: str, sql: str, ctx: Context, max_rows: int = 500) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        r = lyst.data.query(user, workspace, sql, max_rows=min(max_rows, 5000))
        return {"columns": r.columns, "rows": r.rows, "executed_sql": r.executed_sql, "query_id": r.query_id}

    @mcp.tool(description="Findings raised by approved monitors (shifts, forecast deviations, data problems), ranked by priority.")
    @_guarded
    def findings(workspace: str, ctx: Context, status: str | None = None) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [json.loads(json.dumps(f, default=str)) for f in lyst.monitors.findings(user, workspace, status=status)]

    @mcp.tool(description="A task's status, plan, steps and result by id.")
    @_guarded
    def task(task_id: str, ctx: Context) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        t = lyst.runtime.get(user, task_id)
        return json.loads(json.dumps({k: t[k] for k in ("id", "status", "title", "error", "plan", "steps", "result")}, default=str))

    return mcp
