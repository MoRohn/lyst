# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Deliverables from a finished analysis: an Excel workbook and a Markdown briefing.

Both are built from the stored result only (answer, evidence, tables, checks, queries), so a
deliverable always matches what the analyst verified. Files are written under
``$LYST_HOME/artifacts/<workspace>/<task>/`` and recorded in ``artifacts``; the API serves them to
members of the workspace.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .security import Guard, Principal
from .store import Stores

HEADER_FILL = PatternFill("solid", fgColor="1F5EFF")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def _sheet_title(text: str, used: set[str]) -> str:
    base = "".join(c for c in text if c not in '[]:*?/\\')[:28] or "Sheet"
    title, n = base, 2
    while title in used:
        title, n = f"{base[:25]} {n}", n + 1
    used.add(title)
    return title


def _write_table(ws, columns: list[str], rows: list[list[Any]], start_row: int = 1) -> None:
    for j, c in enumerate(columns, start=1):
        cell = ws.cell(row=start_row, column=j, value=str(c))
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="left")
    for i, row in enumerate(rows, start=start_row + 1):
        for j, v in enumerate(row, start=1):
            if isinstance(v, dict | list):
                v = json.dumps(v, default=str)
            ws.cell(row=i, column=j, value=v)
    for j, c in enumerate(columns, start=1):
        width = max([len(str(c))] + [len(str(r[j - 1])) for r in rows[:200] if j - 1 < len(r)]) + 2
        ws.column_dimensions[get_column_letter(j)].width = min(60, max(10, width))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)


def workbook(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    wb = Workbook()
    used: set[str] = set()
    summary = wb.active
    summary.title = _sheet_title("Summary", used)
    lines = [("Question", result.get("question", "")), ("Answer", result.get("answer", "")),
             ("Status", result.get("status", "")), ("Confidence", result.get("confidence", {}).get("level", "")),
             ("Task", task.get("id", "")), ("Generated", datetime.now(UTC).isoformat(timespec="seconds"))]
    for i, (k, v) in enumerate(lines, start=1):
        summary.cell(row=i, column=1, value=k).font = Font(bold=True)
        summary.cell(row=i, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    r = len(lines) + 2
    for heading, items in (("Evidence", result.get("evidence", [])),
                           ("Business implications", result.get("implications", [])),
                           ("Recommended actions", result.get("actions", [])),
                           ("Assumptions", result.get("supporting", {}).get("assumptions", []))):
        if not items:
            continue
        summary.cell(row=r, column=1, value=heading).font = Font(bold=True)
        for item in items:
            r += 1
            summary.cell(row=r, column=2, value=str(item)).alignment = Alignment(wrap_text=True)
        r += 2
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 110
    for t in result.get("tables", []):
        ws = wb.create_sheet(_sheet_title(t["title"], used))
        _write_table(ws, t["columns"], t["rows"])
    for c in result.get("charts", []):
        ws = wb.create_sheet(_sheet_title(c["title"], used))
        cols = ["period", "value"] + (["lo", "hi"] if c.get("lo") else [])
        rows = [[x, y] + ([lo, hi] if c.get("lo") else [])
                for x, y, lo, hi in zip(c["x"], c["y"], c.get("lo") or [None] * len(c["x"]),
                                        c.get("hi") or [None] * len(c["x"]), strict=True)]
        _write_table(ws, cols, rows)
    checks = result.get("confidence", {}).get("checks", [])
    if checks:
        ws = wb.create_sheet(_sheet_title("Checks", used))
        _write_table(ws, ["check", "passed", "severity", "detail"],
                     [[c["name"], c["passed"], c["severity"], c["detail"]] for c in checks])
    queries = result.get("supporting", {}).get("queries", [])
    if queries:
        ws = wb.create_sheet(_sheet_title("Queries", used))
        _write_table(ws, ["purpose", "query_id", "rows", "ms", "datasets", "sql"],
                     [[q["purpose"], q["query_id"], q["rows"], q["ms"], ", ".join(q["datasets"]), q["sql"]] for q in queries])
    import io

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def briefing(result: dict[str, Any], task: dict[str, Any]) -> str:
    """An executive briefing in Markdown: Answer → Evidence → Implications → Actions → Supporting work."""
    sup = result.get("supporting", {})
    conf = result.get("confidence", {})
    out = [f"# {result.get('question', 'Analysis')}", "",
           f"*Lyst, {datetime.now(UTC).strftime('%d %b %Y')} · task {task.get('id', '')} · "
           f"{conf.get('level', '')}*", "", "## Answer", "", result.get("answer", ""), ""]
    for heading, items in (("Evidence", result.get("evidence", [])),
                           ("Business implications", result.get("implications", [])),
                           ("Recommended actions", result.get("actions", []))):
        if items:
            out += [f"## {heading}", ""] + [f"- {i}" for i in items] + [""]
    for t in result.get("tables", []):
        out += [f"### {t['title']}", "", "| " + " | ".join(map(str, t["columns"])) + " |",
                "|" + "---|" * len(t["columns"])]
        for row in t["rows"][:50]:
            out.append("| " + " | ".join(_cell(v) for v in row) + " |")
        if len(t["rows"]) > 50:
            out.append(f"\n*{len(t['rows']) - 50} more rows in the workbook.*")
        out.append("")
    out += ["## Supporting work", ""]
    if sup.get("assumptions"):
        out += ["**Assumptions**", ""] + [f"- {a}" for a in sup["assumptions"]] + [""]
    if conf.get("checks"):
        out += ["**Checks**", ""] + [f"- {'✓' if c['passed'] else '✕'} {c['name']}: {c['detail']}" for c in conf["checks"]] + [""]
    if sup.get("queries"):
        out += ["**Queries**", ""]
        for q in sup["queries"]:
            out += [f"*{q['purpose']}* (query #{q['query_id']}, {q['rows']} rows)", "", "```sql", q["sql"], "```", ""]
    if sup.get("reproduce"):
        out += [sup["reproduce"], ""]
    return "\n".join(out)


def _cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"
    return str(v).replace("|", "\\|")


class ArtifactService:
    def __init__(self, stores: Stores, guard: Guard, home: Path, knowledge: Any = None) -> None:
        self.stores = stores
        self.guard = guard
        self.home = home
        self.knowledge = knowledge

    def build(self, actor: Principal, task: dict[str, Any], kind: str) -> dict[str, Any]:
        ws = task["workspace_id"]
        self.guard.require(actor, ws, "workspace.read", task["id"])
        result = task.get("result")
        if not result:
            raise ValueError("the task has no result yet")
        if kind == "xlsx":
            data, name, mime = workbook(result, task), "analysis.xlsx", \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif kind == "md":
            data, name, mime = briefing(result, task).encode(), "briefing.md", "text/markdown"
        else:
            raise ValueError("kind must be xlsx or md")
        folder = self.home / "artifacts" / ws / task["id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        aid = "art_" + uuid.uuid4().hex[:10]
        self.stores.control.execute(
            "INSERT INTO artifacts (id, workspace_id, task_id, kind, path, bytes, sha256, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [aid, ws, task["id"], kind, str(path), len(data), hashlib.sha256(data).hexdigest(), actor.user_id])
        self.guard.audit(actor, ws, "artifact.create", aid, "ok", {"kind": kind, "task": task["id"]})
        if self.knowledge:
            self.knowledge.record_artifact(actor, ws, aid, task["id"], kind)
        return {"id": aid, "kind": kind, "name": name, "mime": mime, "bytes": len(data), "path": str(path)}

    def get(self, actor: Principal, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        rows = self.stores.control.query("SELECT * FROM artifacts WHERE id = ?", [artifact_id])
        if not rows:
            raise KeyError(artifact_id)
        a = rows[0]
        self.guard.require(actor, a["workspace_id"], "workspace.read", artifact_id)
        return a, Path(a["path"]).read_bytes()

    def list(self, actor: Principal, ws: str, task_id: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql = "SELECT id, task_id, kind, bytes, created_by, created_at FROM artifacts WHERE workspace_id = ?"
        params: list[Any] = [ws]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        return self.stores.control.query(sql + " ORDER BY created_at DESC", params)
