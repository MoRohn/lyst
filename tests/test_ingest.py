# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Multimodal ingestion: workbooks, documents, delimited and JSON files read the way a person reads them."""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from lyst.api import create_app
from lyst.ingest import sniff
from lyst.ingest.tables import generalise_formula, split_unit
from lyst.security import AccessDenied


def _with_cached_values(xlsx: bytes, sheet: int, values: dict[str, float]) -> bytes:
    """openpyxl writes formulas without results; a real spreadsheet app saves both. Fill the results in."""
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == f"xl/worksheets/sheet{sheet}.xml":
                text = data.decode()
                for ref, v in values.items():
                    text = re.sub(rf'(<c r="{ref}"[^>]*><f>[^<]*</f>)<v\s*/?>(</v>)?', rf"\g<1><v>{v}</v>", text)
                data = text.encode()
            dst.writestr(item, data)
    return out.getvalue()


def workbook(*, cached: bool = True) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "P&L"
    ws["A1"] = "Northwind regional P&L"
    ws.append([])
    ws.append(["Region", "FY2026", None, None])
    ws.merge_cells("B3:D3")
    ws.append([None, "Revenue (USD m)", "Cost (USD m)", "Margin (USD m)"])
    rows = [("EMEA", 120.5, 80.25), ("Americas", 210.0, 150.0), ("APAC", 95.0, 70.5)]
    for i, (r, a, b) in enumerate(rows, start=5):
        ws.append([r, a, b, f"=B{i}-C{i}"])
    ws.append(["Total", "=SUM(B5:B7)", "=SUM(C5:C7)", "=SUM(D5:D7)"])
    ws.append([])
    ws.append(["Source: management accounts, unaudited."])
    s2 = wb.create_sheet("Headcount")
    s2.append(["Team", "People", "Start"])
    s2.append(["Sales", 12, dt.date(2026, 1, 5)])
    s2.append(["Ops", 7, dt.date(2026, 2, 1)])
    s2.append([])
    s2.append([])
    s2.append(["Office", "Desks"])
    s2.append(["London", 40])
    s2.append(["Austin", 25])
    hidden = wb.create_sheet("Lookups")
    hidden.sheet_state = "hidden"
    hidden.append(["key", "value"])
    hidden.append(["a", 1])
    buf = io.BytesIO()
    wb.save(buf)
    if not cached:
        return buf.getvalue()
    # Total row deliberately wrong for cost (300.0 instead of 300.75) so the check has something to catch
    return _with_cached_values(buf.getvalue(), 1, {"D5": 40.25, "D6": 60.0, "D7": 24.5, "B8": 425.5, "C8": 300.0, "D8": 124.75})


def docx(paragraphs: list[tuple[str, str]], table: list[list[str]]) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>")
    rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>" for r in table)
    body.append(f"<w:tbl>{rows}</w:tbl>")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {w}><w:body>{"".join(body)}</w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return out.getvalue()


def test_workbook_tables_headers_units_formulas_and_totals(seeded):
    lyst, owner = seeded
    book = workbook()
    plan = lyst.ingest.inspect(owner, "northwind", book, filename="regional.xlsx")
    assert plan["kind"] == "spreadsheet" and plan["format"] == "xlsx"
    by_key = {t["key"]: t for t in plan["tables"]}
    pnl = by_key["P&L!A3:D8"]
    assert pnl["title"] == "Northwind regional P&L"
    assert pnl["notes"] == ["Source: management accounts, unaudited."]
    assert pnl["header_rows"] == 2 and pnl["rows"] == 3  # the Total row is not data
    cols = {c["name"]: c for c in pnl["columns"]}
    assert set(cols) == {"region", "fy2026_revenue", "fy2026_cost", "fy2026_margin"}
    assert cols["fy2026_revenue"]["unit"] == "USD" and cols["fy2026_revenue"]["scale"] == 1e6
    assert cols["fy2026_margin"]["derived"] and cols["fy2026_margin"]["formula"] == "=B{row}-C{row}"
    total = pnl["totals"][0]
    assert total["label"] == "Total" and total["matches"] is False
    checks = {c["column"]: c for c in total["checks"]}
    assert checks["fy2026_revenue"]["matches"] and not checks["fy2026_cost"]["matches"]
    assert any("does not equal the sum" in w for w in pnl["warnings"])
    # two separate tables on one sheet, each with its own name
    hc = [t for t in plan["tables"] if t["sheet"] == "Headcount"]
    assert len(hc) == 2 and len({t["proposed_name"] for t in hc}) == 2
    start = next(c for c in hc[0]["columns"] if c["name"] == "start")
    assert start["type"] == "date"
    hidden = next(t for t in plan["tables"] if t["sheet"] == "Lookups")
    assert hidden["include"] is False and any("hidden" in w for w in hidden["warnings"])
    assert next(s for s in plan["sheets"] if s["name"] == "Lookups")["hidden"]

    out = lyst.ingest.commit(owner, "northwind", plan["id"], tables=[{"key": "P&L!A3:D8", "name": "regional_pnl"}])
    names = {d["dataset"] for d in out["datasets"]}
    assert "regional_pnl" in names and "lookups" not in names and len(names) == 3
    r = lyst.data.query(owner, "northwind", "SELECT sum(fy2026_margin) AS m, count(*) AS n FROM regional_pnl")
    assert r.rows[0] == [124.75, 3]
    cat = {d["name"]: d for d in lyst.data.catalog(owner, "northwind")}
    assert cat["regional_pnl"]["source"] == "regional.xlsx#P&L!A3:D8"
    assert "Units:" in cat["regional_pnl"]["description"] and "Derived: fy2026_margin" in cat["regional_pnl"]["description"]
    job = lyst.ingest.get(owner, "northwind", plan["id"])
    assert job["status"] == "committed"
    assert lyst.stores.workspace("northwind").scalar("SELECT content FROM ingest_jobs WHERE id = ?", [plan["id"]]) is None
    with pytest.raises(ValueError, match="already committed"):
        lyst.ingest.commit(owner, "northwind", plan["id"])
    again = lyst.ingest.inspect(owner, "northwind", book, filename="regional copy.xlsx")
    assert again["duplicate_of"]["id"] == plan["id"]


def test_formulas_never_recalculated_are_reported(seeded):
    lyst, owner = seeded
    plan = lyst.ingest.inspect(owner, "northwind", workbook(cached=False), filename="fresh.xlsx")
    pnl = next(t for t in plan["tables"] if t["sheet"] == "P&L")
    assert any("never recalculated" in w for w in pnl["warnings"])


def test_word_document_text_and_tables(seeded):
    lyst, owner = seeded
    content = docx([("Heading1", "Pricing review"),
                    ("", "Hardware list prices rose four percent in July after the supplier increase."),
                    ("Heading2", "Price list")],
                   [["Product", "List price ($)"], ["Router", "1,200.00"], ["Switch", "850.50"], ["Total", "2,050.50"]])
    plan = lyst.ingest.inspect(owner, "northwind", content, filename="pricing.docx")
    assert plan["kind"] == "document" and plan["format"] == "docx"
    assert plan["document"]["headings"] == ["Pricing review", "Price list"]
    t = plan["tables"][0]
    assert t["proposed_name"] == "pricing_price_list" and t["rows"] == 2
    assert t["columns"][1]["type"] == "number" and t["columns"][1]["unit"] == "USD"
    assert t["totals"][0]["matches"] is True
    out = lyst.ingest.commit(owner, "northwind", plan["id"])
    assert out["document"]["passages"] >= 1 and out["datasets"][0]["rows"] == 2
    hits = lyst.knowledge.search(owner, "northwind", "supplier increase hardware prices")
    assert hits and hits[0]["document_id"] == out["document"]["id"]
    rel = lyst.knowledge.relations(owner, "northwind", "document", out["document"]["id"])
    assert any(r["rel"] == "contains" and r["to_name"] == "pricing_price_list" for r in rel)


def test_html_markdown_json_and_messy_csv(seeded):
    lyst, owner = seeded
    html = b"""<html><head><style>p{}</style></head><body><h1>Store sales</h1><p>Weekly by store.</p>
    <table><caption>Week 12</caption><tr><th>Store</th><th>Sales</th></tr><tr><td>Leeds</td><td>1,020</td></tr>
    <tr><td>York</td><td>980</td></tr></table><script>var x = 1;</script></body></html>"""
    p = lyst.ingest.inspect(owner, "northwind", html, filename="sales.html")
    assert p["format"] == "html" and p["tables"][0]["title"] == "Week 12" and p["tables"][0]["rows"] == 2
    assert "var x" not in p["document"]["excerpt"]
    md = b"# Targets\n\nQuarterly targets.\n\n| Quarter | Target |\n|---|---:|\n| Q1 | 100 |\n| Q2 | 120 |\n"
    p = lyst.ingest.inspect(owner, "northwind", md, filename="targets.md")
    assert p["format"] == "markdown" and p["tables"][0]["columns"][1]["type"] == "integer"
    js = json.dumps({"meta": {"n": 2}, "orders": [{"id": 1, "customer": {"name": "A"}, "tags": ["x"]},
                                                   {"id": 2, "customer": {"name": "B"}, "tags": []}]}).encode()
    p = lyst.ingest.inspect(owner, "northwind", js, filename="orders.json")
    cols = [c["name"] for c in p["tables"][0]["columns"]]
    assert sorted(cols) == ["customer_name", "id", "tags"] and p["tables"][0]["rows"] == 2
    out = lyst.ingest.commit(owner, "northwind", p["id"])
    assert out["datasets"][0]["dataset"] == "orders"
    # semicolon-delimited, Windows-1252, a title line, European decimals
    csv_bytes = "Export from ERP\nRégion;Montant (€)\nÎle-de-France;1,5\nBretagne;2,25\n".encode("cp1252")
    p = lyst.ingest.inspect(owner, "northwind", csv_bytes, filename="export.csv")
    assert p["delimiter"] == ";" and p["encoding"] == "windows-1252"
    t = p["tables"][0]
    assert t["title"] == "Export from ERP" and t["columns"][0]["header"] == "Région"
    assert t["columns"][1]["unit"] == "EUR" and t["preview"][0][1] == 1.5


def test_slides_and_pdf(seeded):
    from pptx import Presentation
    from pptx.util import Inches
    from reportlab.pdfgen import canvas

    lyst, owner = seeded
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Quarterly bookings"
    shape = slide.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(4), Inches(1.5))
    for r, row in enumerate([["Quarter", "Bookings"], ["Q1", "410"], ["Q2", "455"]]):
        for c, v in enumerate(row):
            shape.table.cell(r, c).text = v
    slide.notes_slide.notes_text_frame.text = "Q2 includes the delayed Acme renewal."
    buf = io.BytesIO()
    prs.save(buf)
    p = lyst.ingest.inspect(owner, "northwind", buf.getvalue(), filename="board.pptx")
    assert p["format"] == "pptx" and p["document"]["headings"] == ["Quarterly bookings"]
    assert p["tables"][0]["proposed_name"] == "board_quarterly_bookings" and p["tables"][0]["rows"] == 2
    assert "Acme renewal" in p["document"]["excerpt"]
    pdf = io.BytesIO()
    c = canvas.Canvas(pdf)
    c.drawString(72, 720, "Churn rose in the SMB segment after the price change in March.")
    c.save()
    p = lyst.ingest.inspect(owner, "northwind", pdf.getvalue(), filename="memo.pdf")
    assert p["format"] == "pdf" and p["document"]["pages"] == 1 and "Churn rose" in p["document"]["excerpt"]
    scanned = io.BytesIO()
    page = canvas.Canvas(scanned)
    page.rect(72, 72, 400, 600, fill=1)  # a page that is only a picture
    page.showPage()
    page.save()
    p = lyst.ingest.inspect(owner, "northwind", scanned.getvalue(), filename="scan.pdf")
    assert any("OCR" in w for w in p["warnings"])


def test_refusals_are_plain(seeded):
    lyst, owner = seeded
    with pytest.raises(ValueError, match="legacy Office"):
        lyst.ingest.inspect(owner, "northwind", b"\xd0\xcf\x11\xe0" + b"\0" * 100, filename="old.xls")
    with pytest.raises(ValueError, match="password-protected"):
        lyst.ingest.inspect(owner, "northwind", b"\xd0\xcf\x11\xe0" + b"\0" * 100, filename="locked.xlsx")
    with pytest.raises(ValueError, match="OCR"):
        lyst.ingest.inspect(owner, "northwind", b"\x89PNG\r\n\x1a\n" + b"\0" * 20, filename="chart.png")
    with pytest.raises(ValueError, match="empty"):
        lyst.ingest.inspect(owner, "northwind", b"", filename="nothing.csv")
    with pytest.raises(ValueError, match="damaged"):
        lyst.ingest.inspect(owner, "northwind", b"PK\x03\x04garbage", filename="broken.xlsx")
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document/>'
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("word/document.xml", bomb)
    with pytest.raises(ValueError, match="entities"):
        lyst.ingest.inspect(owner, "northwind", z.getvalue(), filename="evil.docx")
    assert sniff(workbook(), "renamed.bin") == "xlsx"


def test_viewers_cannot_ingest_and_discard(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.ingest.inspect(viewer, "northwind", b"a,b\n1,2\n", filename="x.csv")
    plan = lyst.ingest.inspect(owner, "northwind", b"a,b\n1,2\n3,4\n", filename="x.csv")
    assert lyst.ingest.discard(owner, "northwind", plan["id"])["status"] == "discarded"
    with pytest.raises(ValueError, match="discarded"):
        lyst.ingest.commit(owner, "northwind", plan["id"])


def test_api_inspect_commit_and_one_call_load(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner, "test")
    with TestClient(create_app(lyst, start_workers=False)) as client:
        h = {"Authorization": f"Bearer {token}"}
        r = client.post("/v1/workspaces/northwind/ingest", headers=h, files={"file": ("regional.xlsx", workbook())})
        assert r.status_code == 200, r.text
        plan = r.json()
        r = client.post(f"/v1/workspaces/northwind/ingest/{plan['id']}/commit", headers=h,
                        json={"tables": [{"key": "Headcount!A6:B8", "include": False}]})
        assert r.status_code == 200, r.text
        assert len(r.json()["datasets"]) == 2
        assert client.get("/v1/workspaces/northwind/ingest", headers=h).json()[0]["status"] == "committed"
        fmts = client.get("/v1/ingest/formats", headers=h).json()
        assert {"xlsx", "csv", "parquet", "docx", "pdf"} <= set(fmts["extensions"]) and "xls" in fmts["refused"]
        assert client.get("/v1/ingest/formats").status_code in (401, 403)
        single = client.post("/v1/workspaces/northwind/ingest", headers=h, files={"file": ("one.tsv", b"a\tb\n1\t2\n")}).json()
        assert single["kind"] == "table" and len(single["tables"]) == 1 and single["delimiter"] == "\t"
        r = client.post("/v1/workspaces/northwind/ingest", headers=h, files={"file": ("q.csv", b"x,y\n1,2\n3,4\n")},
                        data={"commit": "true"})
        assert r.status_code == 200 and r.json()["datasets"][0]["dataset"] == "q"
        r = client.post("/v1/workspaces/northwind/ingest", headers=h, files={"file": ("old.xls", b"\xd0\xcf\x11\xe0" + b"\0" * 64)})
        assert r.status_code == 400 and "legacy" in r.text
        r = client.post(f"/v1/workspaces/northwind/ingest/{plan['id']}/commit", headers=h,
                        json={"tables": [{"key": "nope"}]})
        assert r.status_code == 400


def test_helpers():
    assert split_unit("Revenue (USD m)") == ("Revenue", "USD", 1e6)
    assert split_unit("Margin %") == ("Margin", "%", None)
    assert split_unit("Sales ($k)") == ("Sales", "USD", 1e3)
    assert generalise_formula("=SUM($B5:C5)*D5") == "=SUM($B{row}:C{row})*D{row}"
