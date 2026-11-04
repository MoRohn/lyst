# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workbook intelligence: every table in every sheet, the way the person who built the workbook meant it.

Reads .xlsx/.xlsm with openpyxl twice: once for the saved values and once for the formulas, so a derived column
("Margin = Revenue - Cost") is reported as derived, with its formula, and a formula that was never calculated is
reported instead of being loaded as an empty column without a word. Merged header cells are spread across the
columns they cover. Hidden sheets, rows and columns are reported (hidden rows stay out of the data unless the
person includes them). Old .xls files are not read: say so and ask for .xlsx.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .tables import blank, locate_tables, preview, proposed_name, shape_table

MAX_CELLS = 3_000_000


def read_workbook(content: bytes, filename: str, *, include_hidden_rows: bool = False) -> dict[str, Any]:
    if filename.lower().endswith(".xls") and not content.startswith(b"PK"):
        raise ValueError(f"{filename} is an Excel 97-2003 (.xls) workbook, which Lyst does not read. Save it as .xlsx "
                         "(File > Save As > Excel Workbook) and upload that.")
    try:
        values_wb = load_workbook(io.BytesIO(content), data_only=True)
        formula_wb = load_workbook(io.BytesIO(content), data_only=False)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError(f"{filename} could not be opened as a workbook ({exc}). Nothing was loaded.") from exc
    sheets, warnings, tables = [], [], []
    stem = filename.rsplit(".", 1)[0]
    total_cells = 0
    for ws in values_wb.worksheets:
        fws = formula_wb[ws.title]
        sheet: dict[str, Any] = {"name": ws.title, "hidden": ws.sheet_state != "visible", "tables": [],
                                 "dimensions": ws.dimensions}
        max_row, max_col = ws.max_row or 0, ws.max_column or 0
        total_cells += max_row * max_col
        if total_cells > MAX_CELLS:
            warnings.append(f"The workbook has more than {MAX_CELLS:,} cells; sheets from {ws.title} on were not read.")
            break
        hidden_rows = {i for i, d in ws.row_dimensions.items() if d.hidden}
        hidden_cols = {k for k, d in ws.column_dimensions.items() if d.hidden}
        grid = [[c.value for c in row] for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col)]
        fgrid = [[(c.value if isinstance(c.value, str) and c.value.startswith("=") else None) for c in row]
                 for row in fws.iter_rows(min_row=1, max_row=max_row, max_col=max_col)]
        merged_header_rows: set[int] = set()
        for rng in ws.merged_cells.ranges:
            v = grid[rng.min_row - 1][rng.min_col - 1]
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if rng.max_col > rng.min_col and r == rng.min_row:  # spread a horizontal merge across its columns
                        grid[r - 1][c - 1] = v
                        merged_header_rows.add(r - 1)
        if hidden_rows and not include_hidden_rows:
            for i in hidden_rows:
                if 0 < i <= len(grid):
                    grid[i - 1] = [None] * max_col
                    fgrid[i - 1] = [None] * max_col
            sheet["hidden_rows"] = len(hidden_rows)
        if hidden_cols:
            sheet["hidden_columns"] = sorted(hidden_cols)
        regions = locate_tables(grid)
        for n, reg in enumerate(regions, start=1):
            cells = [row[reg.left:reg.right] for row in grid[reg.top:reg.bottom]]
            fcells = [row[reg.left:reg.right] for row in fgrid[reg.top:reg.bottom]]
            if sum(1 for r in cells for v in r if not blank(v)) < 2:
                continue
            shaped = shape_table(cells, formulas=fcells, merged_header=reg.top in merged_header_rows)
            ref = f"{get_column_letter(reg.left + 1)}{reg.top + 1}:{get_column_letter(reg.right)}{reg.bottom}"
            key = f"{ws.title}!{ref}"
            many = len(regions) > 1
            t = {"key": key, "sheet": ws.title, "range": ref, "title": reg.title, "notes": reg.notes,
                 "proposed_name": proposed_name(stem if len(values_wb.worksheets) == 1 and not many else None,
                                                ws.title if len(values_wb.worksheets) > 1 or not many else None,
                                                (reg.title or f"table_{n}") if many else None),
                 "rows": len(shaped.frame), "columns": shaped.columns, "header_rows": shaped.header_rows,
                 "totals": shaped.totals, "warnings": list(shaped.warnings), "preview": preview(shaped.frame),
                 "include": not sheet["hidden"] and len(shaped.frame) > 0}
            if sheet["hidden"]:
                t["warnings"].append("This sheet is hidden in the workbook; it is not loaded unless you include it.")
            for tot in shaped.totals:
                if tot.get("matches") is False:
                    bad = ", ".join(c["column"] for c in tot["checks"] if not c["matches"])
                    t["warnings"].append(f"The row '{tot['label']}' does not equal the sum of the rows above it for {bad}; "
                                         "the total row is left out of the data either way.")
            sheet["tables"].append(t["key"])
            tables.append((t, shaped.frame))
        sheets.append(sheet)
    if not tables:
        warnings.append("No tables were found in the workbook: every sheet is empty or holds single values only.")
    names = [t["proposed_name"] for t, _ in tables]
    for t, _ in tables:
        if names.count(t["proposed_name"]) > 1:
            t["proposed_name"] = proposed_name(t["sheet"], t["range"].split(":")[0])
    return {"kind": "spreadsheet", "sheets": sheets, "tables": tables, "warnings": warnings}
