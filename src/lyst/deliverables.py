# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Deliverables from a finished analysis: a PDF briefing, slides, an Excel workbook and Markdown.

Each is built from the stored result only, through ``answer_view`` (the same reading the answer page uses), so a
file carries everything the answer shows on screen: the headline, the charts (forecast, interval and history), the
artifacts made in the answer, the evidence, the checks, the assumptions and the method, and always matches what the
analyst verified. Files are written under
``$LYST_HOME/artifacts/<workspace>/<task>/`` and recorded in ``artifacts``; the API serves them to
members of the workspace.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import answer_view as av
from .answer_view import ACCENT, BORDER, GOOD, MUTED, SURFACE, TEXT, WARN
from .security import Guard, Principal
from .store import Stores

HEADER_FILL = PatternFill("solid", fgColor=ACCENT.lstrip("#"))
HEADER_FONT = Font(bold=True, color="FFFFFF")
KICKER_FONT = Font(bold=True, size=9, color=ACCENT.lstrip("#"))
MUTED_FONT = Font(size=9, color=MUTED.lstrip("#"))
WRAP = Alignment(wrap_text=True, vertical="top")


def _stamp(result: dict[str, Any], task: dict[str, Any], *, level: bool = True) -> str:
    return " · ".join(p for p in ("Lyst", datetime.now(UTC).strftime("%d %b %Y"), f"task {task.get('id', '')}" if task.get("id") else "",
                                  av.level_label(result) if level else "") if p)


def _sheet_title(text: str, used: set[str]) -> str:
    base = "".join(c for c in text if c not in '[]:*?/\\')[:28] or "Sheet"
    title, n = base, 2
    while title in used:
        title, n = f"{base[:25]} {n}", n + 1
    used.add(title)
    return title


def _number_format(kind: str, values: list[Any]) -> str | None:
    if kind == "percent":
        return "0.0%"
    if kind == "currency":
        return '"$"#,##0.00' if all(abs(v) < 1000 for v in values if isinstance(v, int | float)) else '"$"#,##0'
    nums = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if not nums:
        return None
    if all(isinstance(v, int) for v in nums):
        return "#,##0"
    return "#,##0.00" if max(abs(v) for v in nums) >= 1 else "0.0000"


def _excel_value(v: Any) -> Any:
    if isinstance(v, dict | list):
        return json.dumps(v, default=str)
    if isinstance(v, str) and len(v) == 10 and (d := av.as_date(v)):
        return d
    return v


def _write_table(ws, columns: list[str], rows: list[list[Any]], start_row: int = 1, *, kinds: list[str] | None = None,
                 headings: bool = True) -> None:
    kinds = kinds or [av.column_kind(c) for c in columns]
    for j, c in enumerate(columns, start=1):
        cell = ws.cell(row=start_row, column=j, value=av.heading(c) if headings else str(c))
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center")
    for j in range(1, len(columns) + 1):
        values = [r[j - 1] for r in rows if j - 1 < len(r)]
        nf = _number_format(kinds[j - 1], values)
        for i, row in enumerate(rows, start=start_row + 1):
            if j - 1 >= len(row):
                continue
            v = _excel_value(row[j - 1])
            cell = ws.cell(row=i, column=j, value=v)
            if isinstance(v, date):
                cell.number_format = "mmm yyyy" if v.day == 1 else "yyyy-mm-dd"
            elif nf and isinstance(v, int | float) and not isinstance(v, bool):
                cell.number_format = nf
            if isinstance(v, str) and len(v) > 60:
                cell.alignment = WRAP
        width = max([len(av.heading(columns[j - 1]))] + [len(av.cell(r[j - 1], kinds[j - 1])) for r in rows[:200] if j - 1 < len(r)]) + 2
        ws.column_dimensions[get_column_letter(j)].width = min(70, max(11, width))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(columns))}{start_row + len(rows)}"


def _sheet_head(ws, title: str, note: str | None = None) -> int:
    """A sheet's title and a muted line under it; returns the first free row (a blank row follows)."""
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14, color=TEXT.lstrip("#"))
    if note:
        ws.cell(row=2, column=1, value=note).font = MUTED_FONT
    return 4 if note else 3


def workbook(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    """The answer as a workbook: a Summary sheet laid out like the answer page (headline, evidence, the chart,
    what it means and what to do, the artifacts made, what stands behind it), one sheet per chart with its data
    and a native Excel chart, one sheet per table, and the method: hypotheses, checks, sources, queries."""
    from openpyxl.worksheet.hyperlink import Hyperlink  # noqa: PLC0415

    from .export_charts import excel_chart, series_for_native  # noqa: PLC0415

    wb = Workbook()
    used: set[str] = set()
    summary = wb.active
    summary.title = _sheet_title("Summary", used)
    sup = result.get("supporting") or {}
    conf = result.get("confidence") or {}
    charts = av.charts(result)

    # sheets first, so the Summary can link to them
    chart_sheets: list[tuple[str, dict[str, Any], dict[str, int], int, int]] = []
    for c in charts:
        ws = wb.create_sheet(_sheet_title(c.get("title") or "Chart", used))
        start = _sheet_head(ws, str(c.get("title", "Chart")), av.chart_alt(c))
        cols, rows = av.chart_rows(c)
        kind = c.get("format", "number")
        _write_table(ws, cols, rows, start, kinds=["number"] + [kind] * (len(cols) - 1))
        last = start + len(rows)
        if c.get("type") == "forecast":
            s = series_for_native(c)
            helper = {"base": 13, "width": 14, "actual": 15, "forecast": 16}
            names = {"base": "Interval base", "width": "Interval", "actual": "Actual", "forecast": "Forecast"}
            for key, col in helper.items():
                ws.cell(row=start, column=col, value=names[key]).font = MUTED_FONT
                for i, v in enumerate(s[key], start=start + 1):
                    ws.cell(row=i, column=col, value=v)
                ws.column_dimensions[get_column_letter(col)].hidden = True
            colmap = helper
        elif c.get("series"):
            colmap = {"value": 2, "series": list(range(2, len(cols) + 1))}
        else:
            colmap = {"value": 2}
        chart = excel_chart(ws, c, first_row=start + 1, last_row=last, cols=colmap)
        chart.visible_cells_only = False
        ws.add_chart(chart, f"{get_column_letter(len(cols) + 2)}{start}")
        chart_sheets.append((ws.title, c, colmap, start + 1, last))
    table_sheets: list[str] = []
    for t in result.get("tables", []):
        ws = wb.create_sheet(_sheet_title(t["title"], used))
        start = _sheet_head(ws, t["title"], f"{len(t['rows'])} rows")
        _write_table(ws, t["columns"], t["rows"], start, kinds=av.table_kinds(result, t["columns"]))
        table_sheets.append(ws.title)
    if result.get("hypotheses"):
        ws = wb.create_sheet(_sheet_title("Hypotheses", used))
        start = _sheet_head(ws, av.TITLES["hypotheses"])
        _write_table(ws, ["hypothesis", "verdict", "detail"],
                     [[av.heading(h.get("id", "")), h.get("verdict", ""), h.get("detail", "")] for h in result["hypotheses"]], start)
        ws.column_dimensions["C"].width = 90
    checks = conf.get("checks", [])
    if checks:
        ws = wb.create_sheet(_sheet_title("Checks", used))
        start = _sheet_head(ws, av.TITLES["checks"], f"Basis: {conf['basis']}" if conf.get("basis") else av.trust_line(result))
        _write_table(ws, ["check", "passed", "severity", "detail"],
                     [[c.get("name", ""), bool(c.get("passed")), c.get("severity", ""), c.get("detail", "")] for c in checks], start)
    srcs = av.sources(result)
    if srcs:
        ws = wb.create_sheet(_sheet_title("Sources", used))
        start = _sheet_head(ws, av.TITLES["sources"])
        _write_table(ws, ["dataset", "version", "rows", "data_through", "sha256", "loaded_at"],
                     [[s.get("dataset"), s.get("version"), s.get("rows"), s.get("data_through"), s.get("sha256"),
                       str(s.get("loaded_at") or "")[:19]] for s in srcs], start)
    queries = sup.get("queries", [])
    if queries or sup.get("plan"):
        ws = wb.create_sheet(_sheet_title("Queries", used))
        start = _sheet_head(ws, av.TITLES["method"], "Plan: " + av.plan_line(result) if sup.get("plan") else None)
        if queries:
            _write_table(ws, ["purpose", "query_id", "rows", "ms", "datasets", "sql"],
                         [[q.get("purpose"), q.get("query_id"), q.get("rows"), q.get("ms"), ", ".join(q.get("datasets") or []),
                           q.get("sql")] for q in queries], start)
            ws.column_dimensions["F"].width = 100
            for i in range(start + 1, start + 1 + len(queries)):
                ws.cell(row=i, column=6).alignment = WRAP
                ws.cell(row=i, column=6).font = Font(name="Consolas", size=9)
        if rp := av.reproduce_line(result):
            ws.cell(row=start + len(queries) + 2, column=1, value=rp).font = MUTED_FONT
    if sup.get("citations"):
        ws = wb.create_sheet(_sheet_title("Documents", used))
        start = _sheet_head(ws, av.TITLES["citations"])
        _write_table(ws, ["title", "passage", "quote"],
                     [[ci.get("title"), ci.get("passage"), ci.get("quote")] for ci in sup["citations"]], start)
        ws.column_dimensions["C"].width = 100
    ex = sup.get("expertise") or {}
    if ex.get("rules"):
        ws = wb.create_sheet(_sheet_title("Expertise", used))
        start = _sheet_head(ws, "Expertise applied", str(ex.get("note") or ""))
        keys = list(dict.fromkeys(k for rule in ex["rules"] for k in rule))
        _write_table(ws, keys, [[rule.get(k) for k in keys] for rule in ex["rules"]], start)

    # the Summary, in the order of the answer page
    ws = summary
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 100
    ws.cell(row=1, column=1, value=result.get("question", "Analysis")).font = Font(bold=True, size=16, color=TEXT.lstrip("#"))
    ws.cell(row=2, column=1, value=_stamp(result, task)).font = MUTED_FONT
    r = 4

    def put(label: str | None, text: Any, *, font: Font | None = None, fill: PatternFill | None = None,
            link: str | None = None) -> None:
        nonlocal r
        if label:
            ws.cell(row=r, column=1, value=label).font = KICKER_FONT
            ws.cell(row=r, column=1).alignment = Alignment(vertical="top")
        cell = ws.cell(row=r, column=2, value=_excel_value(text) if not isinstance(text, str) else text)
        cell.alignment = WRAP
        if font:
            cell.font = font
        if fill:
            cell.fill = fill
        if link:
            cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{link}'!A1", display=str(text))
            cell.font = Font(color=ACCENT.lstrip("#"), underline="single")
        lines = sum(max(1, -(-len(part) // 105)) for part in str(text).split("\n"))
        if lines > 1 or (font and font.size and font.size > 11):
            ws.row_dimensions[r].height = max(15, lines * (18 if font and font.size and font.size > 11 else 15))
        r += 1

    def items(label: str, values: list[Any], **kw: Any) -> None:
        nonlocal r
        for i, v in enumerate(values):
            put(label if i == 0 else None, "• " + str(v), **kw)
        r += 1

    # key facts (stable cells other tools read): the headline and its standing
    for k, v in (("Question", result.get("question", "")), ("Answer", result.get("answer", "")),
                 ("Status", result.get("status", "")), ("Confidence", conf.get("level", "")),
                 ("Task", task.get("id", "")), ("Generated", datetime.now(UTC).isoformat(timespec="seconds"))):
        if k == "Answer":
            put(k, v, font=Font(bold=True, size=13, color=TEXT.lstrip("#")))
        else:
            put(k, v)
    if result.get("brief") and result["brief"] != result.get("answer"):
        put("Brief", result["brief"])
    if tl := av.trust_line(result):
        put("Trust", tl)
    if wn := av.workflow_note(result):
        put("Reuse", wn)
    r += 1
    if cav := av.caveats(result):
        items("Read first", cav, fill=PatternFill("solid", fgColor="FFF4E0"))
    if result.get("evidence"):
        items(av.TITLES["evidence"], result["evidence"])
    if chart_sheets:  # the chart in the flow of the finding, as on screen
        title, c, colmap, first, last = chart_sheets[0]
        ws.cell(row=r, column=1, value=av.TITLES["charts"]).font = KICKER_FONT
        cp = excel_chart(wb[title], c, first_row=first, last_row=last, cols=colmap)
        cp.visible_cells_only = False
        ws.add_chart(cp, f"B{r}")
        r += 21
        put(None, av.chart_alt(c), font=MUTED_FONT)
        r += 1
    if result.get("implications"):
        items(av.TITLES["implications"], result["implications"])
    if result.get("actions"):
        items(av.TITLES["actions"], result["actions"])
    if table_sheets:
        for i, name in enumerate(table_sheets):
            put(av.TITLES["tables"] if i == 0 else None, name, link=name)
        r += 1
    made = av.made_here(result)
    if made:
        for i, m in enumerate(made):
            put(av.TITLES["made_here"] if i == 0 else None, f"Chart: {m['title']}", link=chart_sheets[m["index"]][0])
        r += 1
    if result.get("hypotheses"):
        items(av.TITLES["hypotheses"], [f"{av.heading(h.get('id', ''))} · {h.get('verdict', '')} · {h.get('detail', '')}"
                                         for h in result["hypotheses"]])
    rail = av.rail(result)
    if rail:
        ws.cell(row=r, column=1, value=av.TITLES["rail"].upper()).font = Font(bold=True, size=10, color=MUTED.lstrip("#"))
        r += 1
        for head, text in rail:
            put(head, text)
        r += 1
    if sup.get("assumptions"):
        items(av.TITLES["assumptions"], sup["assumptions"])
    if srcs:
        items(av.TITLES["sources"], [av.source_line(s) for s in srcs])
    if checks:
        items(av.TITLES["checks"], [f"{'✓' if c.get('passed') else '✕'} {c.get('name', '')}: {c.get('detail', '')}" for c in checks])
    if sup.get("plan"):
        put(av.TITLES["plan"], av.plan_line(result))
        r += 1
    if queries:
        items(av.TITLES["queries"], [f"{q.get('purpose') or 'Query'} ({av.query_line(q)}) — SQL on the Queries sheet" for q in queries])
    if sup.get("citations"):
        items(av.TITLES["citations"], [" · ".join(str(x) for x in (ci.get("title"), ci.get("quote")) if x) for ci in sup["citations"]])
    if rp := av.reproduce_line(result):
        put(av.TITLES["reproduce"], rp)
    wb.properties.title = str(result.get("question", "Analysis"))
    wb.properties.creator = "Lyst"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------- Markdown

def _md_table(columns: list[str], rows: list[list[Any]], kinds: list[str] | None = None, limit: int | None = None) -> list[str]:
    kinds = kinds or [av.column_kind(c) for c in columns]
    out = ["| " + " | ".join(av.heading(c) for c in columns) + " |",
           "|" + "|".join("---:" if any(isinstance(r[j], int | float) for r in rows if j < len(r)) else "---"
                          for j in range(len(columns))) + "|"]
    for row in rows[:limit] if limit else rows:
        out.append("| " + " | ".join(av.cell(v, kinds[j] if j < len(kinds) else "number").replace("|", "\\|")
                                      for j, v in enumerate(row)) + " |")
    if limit and len(rows) > limit:
        out.append(f"\n*{len(rows) - limit} more rows in the workbook.*")
    return out + [""]


def _md_chart(c: dict[str, Any]) -> list[str]:
    import base64  # noqa: PLC0415

    from .export_charts import svg  # noqa: PLC0415

    data = base64.b64encode(svg(c).encode()).decode()
    cols, rows = av.chart_rows(c)
    kind = c.get("format", "number")
    return ([f"**{c.get('title', 'Chart')}**", "", f"![{av.chart_alt(c)}](data:image/svg+xml;base64,{data})", "",
             f"*{av.chart_alt(c)}*", "", "<details><summary>Chart data</summary>", ""]
            + _md_table(cols, [[av.period_label(r_[0])] + r_[1:] for r_ in rows], ["number"] + [kind] * (len(cols) - 1))
            + ["</details>", ""])


def briefing(result: dict[str, Any], task: dict[str, Any]) -> str:
    """The answer in Markdown, in the order of the answer page, charts embedded as SVG with their data beneath."""
    sup = result.get("supporting") or {}
    conf = result.get("confidence") or {}
    charts = av.charts(result)
    out = [f"# {result.get('question', 'Analysis')}", "", f"*{_stamp(result, task)}*", "", "## Answer", "",
           f"**{result.get('answer', '')}**", ""]
    if result.get("brief") and result["brief"] != result.get("answer"):
        out += [result["brief"], ""]
    for line in (av.trust_line(result), av.workflow_note(result)):
        if line:
            out += [f"> {line}", ""]
    if cav := av.caveats(result):
        out += ["> **Read first**", ">"] + [f"> - {c}" for c in cav] + [""]
    if result.get("evidence"):
        out += ["## Evidence", ""] + [f"- {i}" for i in result["evidence"]] + [""]
    for c in charts:  # every chart at full size, once, in the answer's flow
        out += _md_chart(c)
    for key in ("implications", "actions"):
        if result.get(key):
            out += [f"## {av.TITLES[key]}", ""] + [f"- {i}" for i in result[key]] + [""]
    for t in result.get("tables", []):
        out += [f"### {t['title']}", ""] + _md_table(t["columns"], t["rows"], av.table_kinds(result, t["columns"]), limit=50)
    if result.get("hypotheses"):
        icons = {"supported": "✓", "partial": "◐", "refuted": "✕", "untestable": "–"}
        out += [f"## {av.TITLES['hypotheses']}", ""] + [
            f"- {icons.get(h.get('verdict'), '·')} **{av.heading(h.get('id', ''))}** · {h.get('verdict', '')} · {h.get('detail', '')}"
            for h in result["hypotheses"]] + [""]
    made = av.made_here(result)
    if made:
        # a list of what the answer produced; each chart is drawn once, above, not repeated here
        out += [f"## {av.TITLES['made_here']}", ""] + [f"- Chart · {m['title']} (above)" for m in made] + [""]
    if rail := av.rail(result):
        out += [f"## {av.TITLES['rail']}", ""] + [f"- **{h}:** {t}" for h, t in rail] + [""]
    out += [f"## {av.TITLES['method']}", ""]
    if sup.get("assumptions"):
        out += [f"### {av.TITLES['assumptions']}", ""] + [f"- {a}" for a in sup["assumptions"]] + [""]
    if srcs := av.sources(result):
        out += [f"### {av.TITLES['sources']}", ""] + [f"- {av.source_line(s)}" for s in srcs] + [""]
    if sup.get("citations"):
        out += [f"### {av.TITLES['citations']}", ""]
        for ci in sup["citations"]:
            out += [f"- {ci.get('title', '')}" + (f", passage {ci['passage']}" if ci.get("passage") is not None else "")]
            if ci.get("quote"):
                out += [f"  > {ci['quote']}"]
        out += [""]
    if conf.get("checks"):
        out += [f"### {av.TITLES['checks']}" + (f" ({conf['basis']})" if conf.get("basis") else ""), ""]
        out += [f"- {'✓' if c.get('passed') else '✕'} **{c.get('name', '')}** ({c.get('severity', '')}): {c.get('detail', '')}"
                for c in conf["checks"]] + [""]
    if sup.get("plan"):
        out += [f"### {av.TITLES['plan']}", "", " → ".join(f"`{p.get('capability', '?')}`" for p in sup["plan"]), ""]
    if sup.get("queries"):
        out += [f"### {av.TITLES['queries']}", ""]
        for q in sup["queries"]:
            out += [f"**{q.get('purpose') or 'Query'}** · {av.query_line(q)}", ""]
            if q.get("sql"):
                out += ["```sql", q["sql"], "```", ""]
    if rp := av.reproduce_line(result):
        out += [f"### {av.TITLES['reproduce']}", "", rp, ""]
    return "\n".join(out)


def _cell(v: Any) -> str:
    return av.cell(v)


# ---------------------------------------------------------------------------- PDF

def pdf_briefing(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    """The answer as a PDF, laid out like the answer page: the headline, evidence, the chart (vector), what it
    means and what to do, tables, hypotheses, the artifacts made, what stands behind it, and the full method."""
    from xml.sax.saxutils import escape  # noqa: PLC0415

    from reportlab.lib import colors  # noqa: PLC0415
    from reportlab.lib.pagesizes import A4  # noqa: PLC0415
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: PLC0415
    from reportlab.lib.units import mm  # noqa: PLC0415
    from reportlab.platypus import (  # noqa: PLC0415
        CondPageBreak,
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from .export_charts import drawing  # noqa: PLC0415

    col = colors.HexColor
    styles = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=col(TEXT))
    h1 = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=23, spaceAfter=4)
    kicker = ParagraphStyle("kicker", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=col(ACCENT),
                            spaceBefore=12, spaceAfter=4)
    headline = ParagraphStyle("headline", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=18, spaceAfter=4)
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=col(MUTED))
    bullet = ParagraphStyle("bullet", parent=base, leftIndent=10, bulletIndent=0, spaceAfter=2)
    mono = ParagraphStyle("mono", parent=base, fontName="Courier", fontSize=7.2, leading=9, textColor=col(TEXT),
                          backColor=col(SURFACE), borderPadding=4, spaceBefore=2, spaceAfter=8)
    cellst = ParagraphStyle("cell", parent=base, fontSize=7.5, leading=9.5)
    headst = ParagraphStyle("head", parent=cellst, fontName="Helvetica-Bold", textColor=colors.white)
    width = A4[0] - 36 * mm
    conf = result.get("confidence") or {}
    sup = result.get("supporting") or {}
    charts = av.charts(result)
    e = lambda s: escape(str(s))  # noqa: E731

    def section(key_or_title: str) -> list:
        return [CondPageBreak(40 * mm), Paragraph(e(av.TITLES.get(key_or_title, key_or_title)).upper(), kicker)]

    def bullets(values: list[Any], style=bullet) -> list:
        return [Paragraph(e(v), style, bulletText="•") for v in values]

    def grid(columns: list[str], rows: list[list[Any]], kinds: list[str] | None = None, widths: list[float] | None = None) -> Table:
        kinds = kinds or [av.column_kind(c) for c in columns]
        data = [[Paragraph(e(av.heading(c)), headst) for c in columns]]
        data += [[Paragraph(e(av.cell(v, kinds[j] if j < len(kinds) else "number")), cellst) for j, v in enumerate(r_)] for r_ in rows]
        tbl = Table(data, repeatRows=1, hAlign="LEFT", colWidths=widths)
        tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), col(ACCENT)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LINEBELOW", (0, 0), (-1, -1), 0.25, col(BORDER)),
                                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, col(SURFACE)]),
                                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        return tbl

    def chart_block(c: dict[str, Any], w: float, h: float) -> KeepTogether:
        return KeepTogether([drawing(c, w, h), Spacer(1, 3), Paragraph(e(av.chart_alt(c)), small), Spacer(1, 6)])

    level = av.level_label(result)
    tone = {"Verified": GOOD, "Needs your input": WARN}.get(level, WARN if level else MUTED)
    story: list = [Paragraph(e(result.get("question", "Analysis")), h1)]
    badge = Table([[Paragraph(f'<font color="white"><b>{e(level or "Answer")}</b></font>', small),
                    Paragraph(e(_stamp(result, task, level=False)), small)]], colWidths=[26 * mm, width - 26 * mm], hAlign="LEFT")
    badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), col(tone)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (1, 0), (1, 0), 8)]))
    story += [badge, Spacer(1, 6), Paragraph(av.TITLES["answer"].upper(), kicker), Paragraph(e(result.get("answer", "")), headline)]
    if result.get("brief") and result["brief"] != result.get("answer"):
        story.append(Paragraph(e(result["brief"]), base))
    for line in (av.trust_line(result), av.workflow_note(result)):
        if line:
            story.append(Paragraph(e(line), small))
    if cav := av.caveats(result):
        box = Table([[[Paragraph("<b>Read first</b>", base)] + bullets(cav)]], colWidths=[width])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col("#FFF4E0")), ("BOX", (0, 0), (-1, -1), 0.5, col(WARN))]))
        story += [Spacer(1, 6), box]
    if result.get("evidence"):
        story += section("evidence") + bullets(result["evidence"])
    for c in charts:  # every chart at full size, once, in the answer's flow
        story += [Spacer(1, 8), chart_block(c, width, 82 * mm)]
    if result.get("implications") or result.get("actions"):
        half = (width - 8 * mm) / 2
        left = [Paragraph(av.TITLES["implications"].upper(), kicker)] + bullets(result.get("implications") or []) if result.get("implications") else []
        right = [Paragraph(av.TITLES["actions"].upper(), kicker)] + bullets(result.get("actions") or []) if result.get("actions") else []
        pair = Table([[left, right]], colWidths=[half, half], hAlign="LEFT")
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(pair)
    for t in result.get("tables", []):
        story += section(t["title"]) + [grid(t["columns"], t["rows"][:40], av.table_kinds(result, t["columns"]))]
        if len(t["rows"]) > 40:
            story.append(Paragraph(e(f"{len(t['rows']) - 40} more rows in the workbook."), small))
    if result.get("hypotheses"):
        story += section("hypotheses") + [grid(["hypothesis", "verdict", "detail"],
                                               [[av.heading(h.get("id", "")), h.get("verdict", ""), h.get("detail", "")] for h in result["hypotheses"]],
                                               widths=[38 * mm, 22 * mm, width - 60 * mm])]
    made = av.made_here(result)
    if made:
        # a list of what the answer produced; each chart is drawn once, above, not repeated as a thumbnail here
        story += section("made_here") + bullets([f"Chart · {m['title']} (above)" for m in made])
    if rail := av.rail(result):
        story += section("rail")
        tbl = Table([[Paragraph(f"<b>{e(h)}</b>", cellst), Paragraph(e(t), cellst)] for h, t in rail],
                    colWidths=[38 * mm, width - 38 * mm], hAlign="LEFT")
        tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, col(BORDER))]))
        story.append(tbl)
    story += section("method")
    if sup.get("assumptions"):
        story += [Paragraph(f"<b>{av.TITLES['assumptions']}</b>", base)] + bullets(sup["assumptions"], small)
    if srcs := av.sources(result):
        story += [Paragraph(f"<b>{av.TITLES['sources']}</b>", base)] + bullets([av.source_line(s) for s in srcs], small)
    if sup.get("citations"):
        story.append(Paragraph(f"<b>{av.TITLES['citations']}</b>", base))
        for ci in sup["citations"]:
            story.append(Paragraph(e(" · ".join(str(x) for x in (ci.get("title"), f"passage {ci['passage']}" if ci.get("passage") is not None else None) if x)), small))
            if ci.get("quote"):
                story.append(Paragraph(f"<i>“{e(ci['quote'])}”</i>", small))
    if conf.get("checks"):
        story += [Spacer(1, 4), Paragraph(f"<b>{av.TITLES['checks']}</b>" + (f" ({e(conf['basis'])})" if conf.get("basis") else ""), base),
                  grid(["check", "passed", "severity", "detail"],
                       [[c.get("name", ""), "yes" if c.get("passed") else "NO", c.get("severity", ""), c.get("detail", "")] for c in conf["checks"]],
                       widths=[40 * mm, 14 * mm, 18 * mm, width - 72 * mm])]
    if sup.get("plan"):
        story += [Spacer(1, 4), Paragraph(f"<b>{av.TITLES['plan']}</b> " + e(av.plan_line(result)), base)]
    if sup.get("queries"):
        story += [Spacer(1, 4), Paragraph(f"<b>{av.TITLES['queries']}</b>", base)]
        for q in sup["queries"]:
            story.append(Paragraph(e(f"{q.get('purpose') or 'Query'} · {av.query_line(q)}"), small))
            if q.get("sql"):
                story.append(Paragraph(e(q["sql"]).replace("\n", "<br/>"), mono))
    if rp := av.reproduce_line(result):
        story += [Paragraph(f"<b>{av.TITLES['reproduce']}</b>", base), Paragraph(e(rp), small)]

    title = str(result.get("question", "Analysis"))

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(col(MUTED))
        canvas.drawString(18 * mm, 10 * mm, (title[:90] + "…") if len(title) > 90 else title)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Lyst · page {doc.page}")
        canvas.setStrokeColor(col(ACCENT))
        canvas.setLineWidth(2)
        canvas.line(18 * mm, A4[1] - 10 * mm, 34 * mm, A4[1] - 10 * mm)
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                      title=title, author="Lyst", subject=result.get("answer", "")).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ---------------------------------------------------------------------------- Slides

def deck(result: dict[str, Any], task: dict[str, Any]) -> bytes:
    """The answer as a deck: cover, the answer, evidence, each chart as a native (editable) chart, what it means
    and what to do, tables, hypotheses, the artifacts made, what stands behind it, and the method."""
    from pptx import Presentation  # noqa: PLC0415
    from pptx.dml.color import RGBColor  # noqa: PLC0415
    from pptx.enum.shapes import MSO_SHAPE  # noqa: PLC0415
    from pptx.enum.text import MSO_ANCHOR  # noqa: PLC0415
    from pptx.util import Emu, Inches, Pt  # noqa: PLC0415

    from .export_charts import slide_chart  # noqa: PLC0415

    rgb = lambda hx: RGBColor.from_string(hx.lstrip("#").upper())  # noqa: E731
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    prs.core_properties.title = str(result.get("question", "Analysis"))
    prs.core_properties.author = "Lyst"
    blank = prs.slide_layouts[6]
    conf = result.get("confidence") or {}
    sup = result.get("supporting") or {}
    charts = av.charts(result)
    L, W = Inches(0.7), Inches(11.93)  # noqa: N806

    def text(sl, x, y, w, h, lines: list[Any], size: float = 16, *, bold: bool = False, color: str = TEXT,
             bullet: bool = False, font: str | None = None, anchor=None):
        tf = sl.shapes.add_textbox(x, y, w, h).text_frame
        tf.word_wrap = True
        if anchor is not None:
            tf.vertical_anchor = anchor
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            run = p.add_run()
            run.text = ("•  " if bullet else "") + str(line)
            run.font.size, run.font.bold = Pt(size), bold
            run.font.color.rgb = rgb(color)
            if font:
                run.font.name = font
            p.space_after = Pt(size * 0.5)
        return tf

    def fit(lines: list[Any], big: float, small: float, room: int) -> float:
        n = sum(len(str(x)) for x in lines) + 80 * len(lines)
        return big if n <= room else max(small, big * (room / n) ** 0.5)

    def slide(title: str, kicker: str | None = None):
        sl = prs.slides.add_slide(blank)
        bar = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18), prs.slide_height)
        bar.fill.solid()
        bar.fill.fore_color.rgb = rgb(ACCENT)
        bar.line.fill.background()
        tf = text(sl, L, Inches(0.45), W, Inches(0.9), [title], 28, bold=True)
        tf.paragraphs[0].runs[0].font.size = Pt(28 if len(title) < 60 else 22)
        if kicker:
            text(sl, L, Inches(0.2), W, Inches(0.3), [kicker.upper()], 11, bold=True, color=ACCENT)
        ft = text(sl, L, Inches(7.0), W, Inches(0.3), [f"Lyst · {result.get('question', '')}"[:120]], 9, color=MUTED)
        ft.paragraphs[0].runs[0].font.size = Pt(9)
        return sl

    # 1. cover
    sl = prs.slides.add_slide(blank)
    band = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, Inches(0.3))
    band.fill.solid()
    band.fill.fore_color.rgb = rgb(ACCENT)
    band.line.fill.background()
    text(sl, L, Inches(2.0), W, Inches(2.2), [result.get("question", "Analysis")], 36, bold=True, anchor=MSO_ANCHOR.BOTTOM)
    level = av.level_label(result)
    if level:
        pill = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, L, Inches(4.45), Inches(1.9), Inches(0.42))
        pill.fill.solid()
        pill.fill.fore_color.rgb = rgb({"Verified": GOOD}.get(level, WARN))
        pill.line.fill.background()
        pill.text_frame.text = level
        pill.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        pill.text_frame.paragraphs[0].runs[0].font.bold = True
    text(sl, L + Inches(2.1), Inches(4.45), Inches(9.5), Inches(0.42), [_stamp(result, task, level=False)], 14, color=MUTED)
    if tl := av.trust_line(result):
        text(sl, L, Inches(5.1), W, Inches(0.8), [tl], 14, color=MUTED)

    # 2. the answer
    sl = slide(av.TITLES["answer"])
    lines = [result.get("answer", "")]
    text(sl, L, Inches(1.5), W, Inches(2.2), lines, fit(lines, 26, 18, 260), bold=True)
    y = Inches(3.8)
    if result.get("brief") and result["brief"] != result.get("answer"):
        text(sl, L, y, W, Inches(0.9), [result["brief"]], 16)
        y += Inches(1.0)
    for line in (av.trust_line(result), av.workflow_note(result)):
        if line:
            text(sl, L, y, W, Inches(0.5), [line], 14, color=MUTED)
            y += Inches(0.55)
    if cav := av.caveats(result):
        box = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, L, y + Inches(0.1), W, Inches(1.3))
        box.fill.solid()
        box.fill.fore_color.rgb = rgb("#FFF4E0")
        box.line.color.rgb = rgb(WARN)
        text(sl, L + Inches(0.15), y + Inches(0.15), W - Inches(0.3), Inches(1.2), ["Read first"] + cav, 13)

    # 3. evidence
    if result.get("evidence"):
        sl = slide(av.TITLES["evidence"])
        text(sl, L, Inches(1.5), W, Inches(5.3), result["evidence"], fit(result["evidence"], 20, 12, 900), bullet=True)

    # 4. charts: the chart in the flow first, then every artifact made in this answer
    made = av.made_here(result)
    chart_slides: dict[int, int] = {}
    for i, c in enumerate(charts):
        kick = f"{av.TITLES['made_here']} · Chart" if made else av.TITLES["charts"]
        sl = slide(str(c.get("title", "Chart")), kick)
        slide_chart(sl, c, L, Inches(1.4), W, Inches(4.9))
        text(sl, L, Inches(6.35), W, Inches(0.6), [av.chart_alt(c)], 11, color=MUTED)
        chart_slides[i] = len(prs.slides)

    # 5. what it means and what to do
    if result.get("implications") or result.get("actions"):
        sl = slide(f"{av.TITLES['implications']} and {av.TITLES['actions'].lower()}")
        half = Emu(int((W - Inches(0.5)) / 2))
        for k, x in (("implications", L), ("actions", L + half + Inches(0.5))):
            if result.get(k):
                text(sl, x, Inches(1.45), half, Inches(0.4), [av.TITLES[k].upper()], 12, bold=True, color=ACCENT)
                text(sl, x, Inches(1.9), half, Inches(4.9), result[k], fit(result[k], 18, 11, 600), bullet=True)

    # 6. tables, twelve rows a slide
    for t in result.get("tables", []):
        kinds = av.table_kinds(result, t["columns"])
        pages = [t["rows"][i:i + 12] for i in range(0, max(1, len(t["rows"])), 12)][:6]
        for pi, rows in enumerate(pages):
            sl = slide(t["title"] + (f" ({pi + 1} of {len(pages)})" if len(pages) > 1 else ""), av.TITLES["tables"])
            shape = sl.shapes.add_table(len(rows) + 1, len(t["columns"]), L, Inches(1.45), W, Inches(0.4) * (len(rows) + 1))
            for j, c_ in enumerate(t["columns"]):
                cell_ = shape.table.cell(0, j)
                cell_.text = av.heading(c_)
                cell_.fill.solid()
                cell_.fill.fore_color.rgb = rgb(ACCENT)
                cell_.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
            for i, r_ in enumerate(rows, start=1):
                for j, v in enumerate(r_[:len(t["columns"])]):
                    cell_ = shape.table.cell(i, j)
                    cell_.text = av.cell(v, kinds[j])
                    cell_.fill.solid()
                    cell_.fill.fore_color.rgb = rgb("#FFFFFF" if i % 2 else SURFACE)
                    para = cell_.text_frame.paragraphs[0]
                    if para.runs:
                        para.runs[0].font.size = Pt(11)
                        para.runs[0].font.color.rgb = rgb(TEXT)
        if len(t["rows"]) > 72:
            text(sl, L, Inches(6.5), W, Inches(0.4), [f"{len(t['rows']) - 72} more rows in the workbook."], 11, color=MUTED)

    # 7. hypotheses
    if result.get("hypotheses"):
        sl = slide(av.TITLES["hypotheses"])
        icons = {"supported": "✓", "partial": "◐", "refuted": "✕", "untestable": "–"}
        lines = [f"{icons.get(h.get('verdict'), '·')}  {av.heading(h.get('id', ''))} · {h.get('verdict', '')} · {h.get('detail', '')}"
                 for h in result["hypotheses"]]
        text(sl, L, Inches(1.5), W, Inches(5.3), lines, fit(lines, 16, 10, 1100))

    # 8. made in this answer: each artifact as a card that names its slide
    if made:
        sl = slide(av.TITLES["made_here"])
        cw, gap = Inches(2.8), Inches(0.2)
        for k, m in enumerate(made[:8]):
            x = L + (cw + gap) * (k % 4)
            y = Inches(1.5) + Inches(2.6) * (k // 4)
            card = sl.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, cw, Inches(2.3))
            card.fill.solid()
            card.fill.fore_color.rgb = rgb(SURFACE)
            card.line.color.rgb = rgb(BORDER)
            card.adjustments[0] = 0.06
            text(sl, x + Inches(0.15), y + Inches(0.15), cw - Inches(0.3), Inches(0.3), ["CHART"], 10, bold=True, color=MUTED)
            text(sl, x + Inches(0.15), y + Inches(0.5), cw - Inches(0.3), Inches(1.1), [m["title"]], 16, bold=True)
            text(sl, x + Inches(0.15), y + Inches(1.75), cw - Inches(0.3), Inches(0.4),
                 [f"Slide {chart_slides.get(m['index'], '')}"], 12, color=ACCENT)

    # 9. behind the answer
    if rail := av.rail(result):
        sl = slide(av.TITLES["rail"])
        y = Inches(1.5)
        step = min(Inches(1.05), Emu(int(Inches(5.3) / len(rail))))
        for head, body in rail:
            text(sl, L, y, Inches(2.8), step, [head], 14, bold=True, color=ACCENT)
            text(sl, L + Inches(3.0), y, W - Inches(3.0), step, [body], fit([body], 14, 10, 300))
            y += step

    # 10. the method: assumptions and sources, checks, plan and queries, reproduce
    if sup.get("assumptions") or av.sources(result) or sup.get("citations"):
        sl = slide(f"{av.TITLES['assumptions']} and {av.TITLES['sources'].lower()}", av.TITLES["method"])
        lines = [f"Assumption: {a}" for a in sup.get("assumptions") or []] + [f"Source: {av.source_line(s)}" for s in av.sources(result)]
        lines += [f"Document: {ci.get('title', '')}" + (f" — “{ci['quote']}”" if ci.get("quote") else "") for ci in sup.get("citations") or []]
        text(sl, L, Inches(1.5), W, Inches(5.3), lines, fit(lines, 16, 10, 1100), bullet=True)
    if conf.get("checks"):
        sl = slide(av.TITLES["checks"] + (f" ({conf['basis']})" if conf.get("basis") and len(conf["basis"]) < 50 else ""), av.TITLES["method"])
        rows = conf["checks"][:12]
        shape = sl.shapes.add_table(len(rows) + 1, 4, L, Inches(1.45), W, Inches(0.4) * (len(rows) + 1))
        for j, (h, w_) in enumerate((("Check", 3.0), ("Passed", 1.1), ("Severity", 1.4), ("Detail", 6.43))):
            shape.table.columns[j].width = Inches(w_)
            shape.table.cell(0, j).text = h
            shape.table.cell(0, j).fill.solid()
            shape.table.cell(0, j).fill.fore_color.rgb = rgb(ACCENT)
        for i, c_ in enumerate(rows, start=1):
            for j, v in enumerate((c_.get("name", ""), "✓" if c_.get("passed") else "✕", c_.get("severity", ""), c_.get("detail", ""))):
                cell_ = shape.table.cell(i, j)
                cell_.text = str(v)
                cell_.fill.solid()
                cell_.fill.fore_color.rgb = rgb("#FFFFFF" if i % 2 else SURFACE)
                cell_.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
                cell_.text_frame.paragraphs[0].runs[0].font.color.rgb = rgb(TEXT)
    queries = sup.get("queries") or []
    for qi in range(0, max(len(queries), 1 if sup.get("plan") else 0), 2):
        sl = slide(av.TITLES["queries"] if queries else av.TITLES["plan"], av.TITLES["method"])
        y = Inches(1.45)
        if qi == 0 and sup.get("plan"):
            text(sl, L, y, W, Inches(0.5), [f"{av.TITLES['plan']}: {av.plan_line(result)}"], 14)
            y += Inches(0.6)
        for q in queries[qi:qi + 2]:
            text(sl, L, y, W, Inches(0.4), [f"{q.get('purpose') or 'Query'} · {av.query_line(q)}"], 13, bold=True)
            if q.get("sql"):
                box = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, L, y + Inches(0.45), W, Inches(1.75))
                box.fill.solid()
                box.fill.fore_color.rgb = rgb(SURFACE)
                box.line.fill.background()
                sql = q["sql"] if len(q["sql"]) < 900 else q["sql"][:900] + " …"
                text(sl, L + Inches(0.1), y + Inches(0.5), W - Inches(0.2), Inches(1.65), [sql], fit([sql], 12, 8, 500), font="Consolas")
            y += Inches(2.4)
        if qi + 2 >= len(queries) and (rp := av.reproduce_line(result)):
            text(sl, L, Inches(6.35), W, Inches(0.5), [f"{av.TITLES['reproduce']}: {rp}"], 11, color=MUTED)
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
