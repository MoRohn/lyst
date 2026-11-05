# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst as an MCP server, so other agents and tools can delegate analytical work to it.

Mounted at ``/mcp`` on the API (streamable HTTP, stateless, JSON responses). Every tool call carries
the caller's Lyst bearer token in the ``Authorization`` header and runs under that person's roles:
the same guard, the same audit log, the same workspace isolation as the UI. There is no separate MCP
identity and nothing a tool can do that the API cannot.

Tools: ``ask`` (a question through the analyst, waits for the answer), ``workspaces``, ``datasets``,
``metrics``, ``query`` (guarded read-only SQL), ``findings`` (proactive monitor results), ``task``, ``events``
(what changed since a cursor) and ``ingest_file`` (a file, base64, inspected or loaded).

Hardening: every tool declares MCP annotations (read-only, idempotent, destructive) so clients can gate calls;
every call is audited as ``mcp.<tool>`` with its outcome; results above ``LYST_MCP_MAX_BYTES`` (256 KB) are cut
to fit with ``truncated: true`` rather than flooding the caller's context; files are capped at 20 MB.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .app import Lyst
from .connectors import ConnectorError
from .data.sqlguard import QueryRejected
from .security import AccessDenied, Principal


def _principal(lyst: Lyst, ctx: Context) -> Principal:
    headers: Mapping[str, str] | None = ctx.headers
    auth = (headers or {}).get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    return lyst.guard.authenticate(token)


READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=False)
WORK = ToolAnnotations(read_only_hint=False, idempotent_hint=False, destructive_hint=False, open_world_hint=False)
MAX_FILE = 20 * 1024 * 1024


def _max_bytes() -> int:
    return int(os.environ.get("LYST_MCP_MAX_BYTES", str(256 * 1024)))


def _fit(result: Any) -> Any:
    """Keep a result within the size budget: cut row lists first, then say it was cut."""
    text = json.dumps(result, default=str)
    limit = _max_bytes()
    if len(text) <= limit:
        return result
    if isinstance(result, dict) and isinstance(result.get("rows"), list) and result["rows"]:
        rows = result["rows"]
        keep = max(1, int(len(rows) * limit / len(text) * 0.9))
        return {**result, "rows": rows[:keep], "truncated": True, "total_rows": len(rows),
                "note": f"showing {keep} of {len(rows)} rows; narrow the query or aggregate for the rest"}
    if isinstance(result, dict) and isinstance(result.get("events"), list) and result["events"]:
        evs = result["events"]  # a page of events: keep the oldest that fit and move the cursor to the last one kept
        keep = max(1, int(len(evs) * limit / len(text) * 0.9))
        return {**result, "events": evs[:keep], "next": evs[keep - 1]["seq"], "truncated": True,
                "note": f"showing {keep} of {len(evs)} events; pass `next` back as `after` for the rest"}
    if isinstance(result, list):
        keep = max(1, int(len(result) * limit / len(text) * 0.9))
        return {"items": result[:keep], "truncated": True, "total_items": len(result)}
    return {"truncated": True, "note": f"the result was {len(text):,} bytes, above the {limit:,}-byte limit",
            "excerpt": text[: limit // 2]}


def _guarded(lyst: Lyst):
    """Lyst's own refusals reach the caller as plain text; anything else stays generic. Each call is audited."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx")
            ws = kwargs.get("workspace")
            outcome = "ok"
            try:
                return _fit(fn(*args, **kwargs))
            except (AccessDenied, QueryRejected, ConnectorError, KeyError, ValueError) as exc:
                outcome = "denied" if isinstance(exc, AccessDenied) else "rejected"
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            except Exception:
                outcome = "error"
                raise
            finally:
                try:
                    who = _principal(lyst, ctx) if ctx is not None else None
                except Exception:  # noqa: BLE001 - an unauthenticated call is audited as anonymous
                    who = None
                if who is not None:
                    lyst.guard.audit(who, ws, f"mcp.{fn.__name__}", ws, outcome, {})

        return wrapper

    return deco


def build_mcp(lyst: Lyst) -> MCPServer:
    guarded = _guarded(lyst)
    mcp = MCPServer(
        "lyst", title="Lyst", version="0.1.0",
        instructions="Lyst is a governed AI analyst. Ask it business questions about a workspace's approved metrics; "
                     "every number comes from an executed query and the answer carries its evidence. Pass the user's Lyst "
                     "bearer token as the Authorization header. Start with `workspaces`, then `metrics` to see what is "
                     "approved, then `ask`.")

    @mcp.tool(annotations=READ, description="List the workspaces the caller belongs to, with their role in each.")
    @guarded
    def workspaces(ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return lyst.guard.workspaces_for(user)

    @mcp.tool(annotations=READ, description="Governed datasets in a workspace: name, title, version, rows, columns.")
    @guarded
    def datasets(workspace: str, ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [{k: d[k] for k in ("name", "title", "description", "version", "row_count", "schema")} for d in lyst.data.catalog(user, workspace)]

    @mcp.tool(annotations=READ, description="Approved metric definitions in a workspace, the only numbers Lyst will compute for a business question.")
    @guarded
    def metrics(workspace: str, ctx: Context) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [{"name": m.name, "title": m.title, "description": m.description, "dataset": m.dataset, "kind": m.kind,
                 "dimensions": m.dimensions, "format": m.format, "synonyms": m.synonyms} for m in lyst.semantic.metrics(user, workspace)]

    @mcp.tool(annotations=READ, description="Search the workspace's datasets, columns, metrics, glossary, saved analyses, dashboards and documents "
                          "for a business question; each hit names its directly connected assets.")
    @guarded
    def search(workspace: str, query: str, ctx: Context, limit: int = 20) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        return json.loads(json.dumps(lyst.catalog.search(user, workspace, query, limit=min(limit, 100)), default=str))

    @mcp.tool(annotations=READ, description="Where a metric comes from: definition, compiled SQL, source dataset and origin, contract status, "
                          "quality notes, version history, and the dashboards, workflows and monitors that depend on it.")
    @guarded
    def metric_lineage(workspace: str, metric: str, ctx: Context) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        return json.loads(json.dumps(lyst.catalog.explain_metric(user, workspace, metric), default=str))

    @mcp.tool(annotations=WORK, description="Ask the analyst a question or give it an assignment. Waits up to timeout_seconds for the verified answer "
                          "and returns it with evidence, checks and the queries it ran; returns the task id if still running.")
    @guarded
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

    @mcp.tool(annotations=READ, description="Run one read-only SQL statement against the workspace's governed datasets (by dataset name). "
                          "The same guard applies as in the UI: no writes, no files, column policies by role.")
    @guarded
    def query(workspace: str, sql: str, ctx: Context, max_rows: int = 500) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        r = lyst.data.query(user, workspace, sql, max_rows=min(max_rows, 5000))
        return {"columns": r.columns, "rows": r.rows, "executed_sql": r.executed_sql, "query_id": r.query_id}

    @mcp.tool(annotations=READ, description="Findings raised by approved monitors (shifts, forecast deviations, data problems), ranked by priority.")
    @guarded
    def findings(workspace: str, ctx: Context, status: str | None = None) -> list[dict[str, Any]]:
        user = _principal(lyst, ctx)
        return [json.loads(json.dumps(f, default=str)) for f in lyst.monitors.findings(user, workspace, status=status)]

    @mcp.tool(annotations=READ, description="A task's status, plan, steps and result by id.")
    @guarded
    def task(task_id: str, ctx: Context) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        t = lyst.runtime.get(user, task_id)
        return json.loads(json.dumps({k: t[k] for k in ("id", "status", "title", "error", "plan", "steps", "result")}, default=str))

    @mcp.tool(annotations=READ, description="What changed in a workspace since a cursor: new dataset versions, finished analyses, "
                                            "metric and contract changes. Pass the returned `next` back as `after`; types take "
                                            "patterns such as 'dataset.*'.")
    @guarded
    def events(workspace: str, ctx: Context, after: int = 0, types: list[str] | None = None, limit: int = 50) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        return lyst.events.list(user, workspace, after=after, types=types or None, limit=min(limit, 200))

    @mcp.tool(annotations=WORK, description="Read a file (base64) such as an Excel workbook, Word document, PDF, CSV or JSON. "
                                            "With load=false, returns what is in it (tables, columns, units, totals, text) and "
                                            "loads nothing; with load=true, loads the tables as datasets and the text as "
                                            "searchable knowledge. 20 MB at most.")
    @guarded
    def ingest_file(workspace: str, filename: str, content_base64: str, ctx: Context, load: bool = False) -> dict[str, Any]:
        user = _principal(lyst, ctx)
        if len(content_base64) > MAX_FILE * 4 // 3 + 8:
            raise ValueError("the file is larger than 20 MB; upload it through the API instead")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        if load:
            return json.loads(json.dumps(lyst.ingest.load(user, workspace, content, filename=filename), default=str))
        return json.loads(json.dumps(lyst.ingest.inspect(user, workspace, content, filename=filename), default=str))

    return mcp
