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


# ---------------------------------------------------------------------------- PDF and deck

def pdf_briefing(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    """An executive briefing as PDF (ReportLab): Answer, Evidence, Implications, Actions, tables, Supporting work."""
    import io  # noqa: PLC0415
    from xml.sax.saxutils import escape  # noqa: PLC0415

    from reportlab.lib import colors  # noqa: PLC0415
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: PLC0415
    from reportlab.lib.units import mm  # noqa: PLC0415
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: PLC0415

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, leading=22, spaceAfter=6, alignment=0)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1f5eff"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=14)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#5b6577"))
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=7.5, leading=9)
    conf = result.get("confidence", {})
    sup = result.get("supporting", {})
    story = [Paragraph(escape(result.get("question", "Analysis")), h1),
             Paragraph(escape(f"Lyst · {datetime.now(UTC).strftime('%d %b %Y')} · task {task.get('id', '')} · {conf.get('level', '')}"), small),
             Spacer(1, 6), Paragraph("Answer", h2), Paragraph(escape(result.get("brief") or result.get("answer", "")), body)]
    for heading, items in (("Evidence", result.get("evidence", [])), ("Business implications", result.get("implications", [])),
                           ("Recommended actions", result.get("actions", []))):
        if items:
            story.append(Paragraph(heading, h2))
            story += [Paragraph("• " + escape(str(i)), body) for i in items]
    for t in result.get("tables", []):
        story.append(Paragraph(escape(t["title"]), h2))
        rows = [[str(c) for c in t["columns"]]] + [[_cell(v) for v in r] for r in t["rows"][:25]]
        tbl = Table(rows, repeatRows=1, hAlign="LEFT")
        tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f5eff")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                 ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e3e5ea")),
                                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f6f8")])]))
        story.append(tbl)
        if len(t["rows"]) > 25:
            story.append(Paragraph(escape(f"{len(t['rows']) - 25} more rows in the workbook."), small))
    story.append(Paragraph("Supporting work", h2))
    for a in sup.get("assumptions", []):
        story.append(Paragraph("• " + escape(str(a)), small))
    for c in conf.get("checks", []):
        story.append(Paragraph(escape(f"{'✓' if c['passed'] else '✕'} {c['name']}: {c['detail']}"), small))
    for src in sup.get("sources", []):
        story.append(Paragraph(escape(f"Source {src['dataset']} v{src['version']} · {src['rows']:,} rows · through {src['data_through']} · "
                                      f"sha256 {src['sha256'][:16]}"), small))
    for q in sup.get("queries", [])[:12]:
        story.append(Paragraph(escape(f"{q['purpose']} (query #{q['query_id']}, {q['rows']} rows)"), small))
        story.append(Paragraph(escape(q["sql"]).replace("\n", "<br/>"), mono))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                      title=result.get("question", "Analysis"), author="Lyst").build(story)
    return buf.getvalue()


def deck(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    """A presentation (python-pptx): title, answer, evidence, implications and actions, tables, supporting work."""
    import io  # noqa: PLC0415

    from pptx import Presentation  # noqa: PLC0415
    from pptx.util import Inches, Pt  # noqa: PLC0415

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def slide(title: str, lines: list[str] | None = None, size: int = 18):
        sl = prs.slides.add_slide(blank)
        tb = sl.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(12.1), Inches(1.0)).text_frame
        tb.text = title
        tb.paragraphs[0].runs[0].font.size = Pt(28)
        tb.paragraphs[0].runs[0].font.bold = True
        if lines:
            body = sl.shapes.add_textbox(Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.5)).text_frame
            body.word_wrap = True
            for i, line in enumerate(lines):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                p.text = line
                if p.runs:
                    p.runs[0].font.size = Pt(size)
                p.space_after = Pt(8)
        return sl

    conf = result.get("confidence", {})
    slide(result.get("question", "Analysis"), [f"Lyst · {datetime.now(UTC).strftime('%d %b %Y')} · {conf.get('level', '')}"], 16)
    slide("Answer", [result.get("brief") or result.get("answer", "")], 22)
    if result.get("evidence"):
        slide("Evidence", ["• " + str(e) for e in result["evidence"][:8]], 16)
    if result.get("implications") or result.get("actions"):
        slide("Implications and recommended actions",
              ["Implications"] + ["• " + str(i) for i in result.get("implications", [])] + [""] +
              ["Recommended actions"] + ["• " + str(a) for a in result.get("actions", [])], 16)
    for t in result.get("tables", [])[:4]:
        sl = slide(t["title"])
        rows = t["rows"][:12]
        shape = sl.shapes.add_table(len(rows) + 1, len(t["columns"]), Inches(0.6), Inches(1.5), Inches(12.1),
                                    Inches(0.4) * (len(rows) + 1))
        for j, c in enumerate(t["columns"]):
            shape.table.cell(0, j).text = str(c)
        for i, r in enumerate(rows, start=1):
            for j, v in enumerate(r):
                shape.table.cell(i, j).text = _cell(v)
                para = shape.table.cell(i, j).text_frame.paragraphs[0]
                if para.runs:
                    para.runs[0].font.size = Pt(11)
    sup = result.get("supporting", {})
    lines = [f"{'✓' if c['passed'] else '✕'} {c['name']}: {c['detail']}" for c in conf.get("checks", [])[:10]]
    lines += [f"Source {s_['dataset']} v{s_['version']}, {s_['rows']:,} rows through {s_['data_through']}" for s_ in sup.get("sources", [])]
    lines += [str(a) for a in sup.get("assumptions", [])[:5]]
    slide("Supporting work", lines or ["No checks recorded."], 12)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


KINDS = {
    "xlsx": ("analysis.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "md": ("briefing.md", "text/markdown"),
    "pdf": ("briefing.pdf", "application/pdf"),
    "pptx": ("briefing.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}


class ArtifactService:
    def __init__(self, stores: Stores, guard: Guard, home: Path, knowledge: Any = None) -> None:
        self.before_build: list = []
        self.stores = stores
        self.guard = guard
        self.home = home
        self.knowledge = knowledge
        self.assets: Any = None  # assets.AssetService: every file built is a registered, versioned asset

    def build(self, actor: Principal, task: dict[str, Any], kind: str, *, asset_id: str | None = None) -> dict[str, Any]:
        ws = task["workspace_id"]
        self.guard.require(actor, ws, "workspace.read", task["id"])
        result = task.get("result")
        if not result:
            raise ValueError("the task has no result yet")
        if kind not in KINDS:
            raise ValueError("kind must be one of " + ", ".join(KINDS))
        name, mime = KINDS[kind]
        for check in self.before_build:  # governance.check_export: data policy may forbid files from this result
            check(actor, ws, task["id"])
        data = {"xlsx": lambda: workbook(result, task), "md": lambda: briefing(result, task).encode(),
                "pdf": lambda: pdf_briefing(result, task), "pptx": lambda: deck(result, task)}[kind]()
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
        out = {"id": aid, "kind": kind, "name": name, "mime": mime, "bytes": len(data), "path": str(path)}
        if self.assets is not None:
            reg = self.assets.on_artifact(actor, task, kind, out, hashlib.sha256(data).hexdigest(), asset_id=asset_id)
            if reg:
                out.update(reg)
        return out

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
