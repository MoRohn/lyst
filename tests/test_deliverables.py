# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from lyst.api import create_app

from .conftest import ask


def test_workbook_and_briefing_match_the_verified_result(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    x = lyst.artifacts.build(owner, t, "xlsx")
    meta, data = lyst.artifacts.get(owner, x["id"])
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[0] == "Summary" and "Checks" in wb.sheetnames and "Queries" in wb.sheetnames
    summary = {r[0].value: r[1].value for r in wb["Summary"].iter_rows(min_row=4, max_row=9)}
    assert summary["Answer"] == t["result"]["answer"] and summary["Confidence"] == "verified"
    table_sheet = next(n for n in wb.sheetnames if n.startswith("Change in Operating"))
    rows = [r for r in wb[table_sheet].iter_rows(min_row=4, values_only=True)]
    assert rows[0][0] == "Region" and {r[0] for r in rows[1:]} == {"EMEA", "North America", "APAC", "LATAM"}
    m = lyst.artifacts.build(owner, t, "md")
    _, md = lyst.artifacts.get(owner, m["id"])
    text = md.decode()
    assert text.startswith("# Why did operating margin decline last quarter?")
    for heading in ("## Answer", "## Evidence", "## What it means", "## What to do", "## Method, checks and queries"):
        assert heading in text
    assert "```sql" in text and "EMEA" in text


def test_artifacts_respect_workspace_membership(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    a = lyst.artifacts.build(owner, t, "md")
    outsider = lyst.guard.create_user("o@else.test", "O")
    ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    hx = {"Authorization": f"Bearer {lyst.guard.issue_token(outsider)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get(f"/v1/artifacts/{a['id']}", headers=hx).status_code == 403
        r = c.get(f"/v1/artifacts/{a['id']}", headers=ho)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        made = c.post(f"/v1/tasks/{t['id']}/artifacts/xlsx", headers=ho).json()
        assert made["bytes"] > 1000 and len(c.get(f"/v1/tasks/{t['id']}/artifacts", headers=ho).json()) == 2


def test_pdf_and_deck_carry_the_verified_result(seeded):
    from pptx import Presentation
    from pypdf import PdfReader

    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter? Prepare a briefing for the board.")
    p = lyst.artifacts.build(owner, t, "pdf")
    _, pdf = lyst.artifacts.get(owner, p["id"])
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "ANSWER" in text and "EMEA" in text and "METHOD, CHECKS AND QUERIES" in text and "sha256" in text
    assert "32.5%" in text
    d = lyst.artifacts.build(owner, t, "pptx")
    _, pptx = lyst.artifacts.get(owner, d["id"])
    prs = Presentation(io.BytesIO(pptx))
    titles = [sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame]
    assert "Answer" in titles and "Evidence" in titles and "Queries" in titles
    assert any("EMEA" in sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    with pytest.raises(ValueError):
        lyst.artifacts.build(owner, t, "docx")
    assert {a["kind"] for a in lyst.artifacts.list(owner, "northwind", t["id"])} == {"pdf", "pptx"}


# ---------------------------------------------------------------------------- parity with the answer on screen

def _norm(text: str) -> str:
    return "".join(str(text).split()).lower()


def _expected(r: dict) -> dict[str, list[str]]:
    """For each element the answer page shows, words that must appear in every download made from it."""
    from lyst import answer_view as av

    sup, conf = r.get("supporting") or {}, r.get("confidence") or {}
    want: dict[str, list[str]] = {
        "answer": [r["answer"]],
        "evidence": [av.TITLES["evidence"]] + list(r.get("evidence") or []),
        "charts": [c["title"] for c in av.charts(r)],
        "implications": [av.TITLES["implications"]] + list(r.get("implications") or []),
        "actions": [av.TITLES["actions"]] + list(r.get("actions") or []),
        "tables": [t["title"] for t in r.get("tables") or []],
        "hypotheses": [av.TITLES["hypotheses"]] + [av.heading(h.get("id", "")) for h in r.get("hypotheses") or []],
        "made_here": [av.TITLES["made_here"]] + [m["title"] for m in av.made_here(r)],
        "rail": [av.TITLES["rail"]] + [h for h, _ in av.rail(r)],
        "assumptions": list(sup.get("assumptions") or []),
        "sources": [str(s["dataset"]) for s in av.sources(r)],
        "citations": [str(ci.get("title", "")) for ci in sup.get("citations") or []],
        "checks": [c["name"] for c in conf.get("checks") or []],
        "plan": [str(p.get("capability")) for p in sup.get("plan") or []],
        "queries": [q["sql"][:60] for q in sup.get("queries") or [] if q.get("sql")],
        "reproduce": [str(sup.get("reproduce", ""))[:60]],
    }
    return {k: v for k, v in want.items() if k in av.present(r)}


def _texts(lyst, owner, t) -> dict[str, str]:
    from openpyxl import load_workbook
    from pptx import Presentation
    from pypdf import PdfReader

    out = {}
    _, md = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "md")["id"])
    out["md"] = md.decode()
    _, pdf = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pdf")["id"])
    out["pdf"] = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    _, pptx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pptx")["id"])
    bits = []
    for s in Presentation(io.BytesIO(pptx)).slides:
        for sh in s.shapes:
            if sh.has_text_frame:
                bits.append(sh.text_frame.text)
            if sh.has_table:
                bits += [c.text for row in sh.table.rows for c in row.cells]
            if sh.has_chart:
                bits += [sh.chart.plots[0].categories[0] if len(sh.chart.plots[0].categories) else ""]
    out["pptx"] = " ".join(bits)
    _, xlsx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "xlsx")["id"])
    wb = load_workbook(io.BytesIO(xlsx))
    out["xlsx"] = " ".join(wb.sheetnames) + " " + " ".join(
        str(v) for ws in wb for row in ws.iter_rows(values_only=True) for v in row if v is not None)
    return out


@pytest.mark.parametrize("question", ["Forecast operating margin for the next 2 quarters",
                                      "Why did operating margin decline last quarter?",
                                      "What was revenue last quarter by region?"])
def test_every_download_carries_every_element_the_answer_shows(seeded, question):
    lyst, owner = seeded
    t = ask(lyst, owner, question)
    want = _expected(t["result"])
    assert {"answer", "checks", "queries", "rail"} <= set(want)
    texts = {k: _norm(v) for k, v in _texts(lyst, owner, t).items()}
    missing = [(fmt, element, w) for fmt, text in texts.items() for element, words in want.items()
               for w in words if w and _norm(w) not in text]
    assert not missing, missing


def test_forecast_downloads_carry_the_chart_with_history_forecast_and_interval(seeded):
    from openpyxl import load_workbook
    from pptx import Presentation
    from pypdf import PdfReader

    lyst, owner = seeded
    t = ask(lyst, owner, "Forecast operating margin for the next 2 quarters")
    c = next(c for c in t["result"]["charts"] if c["type"] == "forecast")
    n_hist, n_fc = len(c["history_x"]), len(c["x"])
    assert n_hist > 12 and n_fc >= 6

    # Excel: a sheet with the actual history and the forecast with its interval, as numbers formatted as percents,
    # and a native chart on it and on the Summary
    _, xlsx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "xlsx")["id"])
    wb = load_workbook(io.BytesIO(xlsx))
    ws = wb[c["title"][:28]]
    rows = [r for r in ws.iter_rows(min_row=4, max_col=5, values_only=True) if r[0] is not None]
    assert [*rows[0]] == ["Period", "Actual", "Forecast", "Low", "High"]
    assert len(rows) - 1 == n_hist + n_fc
    assert sum(1 for r in rows[1:] if r[1] is not None) == n_hist and sum(1 for r in rows[1:] if r[3] is not None) == n_fc
    assert ws.cell(row=5, column=2).number_format == "0.0%"
    assert len(ws._charts) == 1 and len(wb["Summary"]._charts) == 1
    names = [s.tx.strRef.f for s in ws._charts[0].series] + [s.tx.strRef.f for ch in ws._charts[0]._charts for s in ch.series]
    assert len(names) >= 4  # interval base and width, actual, forecast

    # Slides: a native chart with the actual line, the forecast and the shaded interval
    _, pptx = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pptx")["id"])
    charts = [sh.chart for s in Presentation(io.BytesIO(pptx)).slides for sh in s.shapes if sh.has_chart]
    assert charts
    xml = charts[0]._chartSpace.xml
    assert "<c:areaChart>" in xml and "<c:lineChart>" in xml
    series = {s.name for p in charts[0].plots for s in p.series}
    assert {"Actual", "Forecast", "Interval"} <= series

    # PDF: the chart is drawn (its legend and axis are in the page), and Markdown embeds it as an image
    _, pdf = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "pdf")["id"])
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Actual" in text and "Interval" in text and "Forecast" in text and "Jan 2025" in text
    _, md = lyst.artifacts.get(owner, lyst.artifacts.build(owner, t, "md")["id"])
    assert md.decode().count("data:image/svg+xml;base64,") >= 2  # in the answer and under Made in this answer


def test_the_answer_page_and_the_downloads_read_an_answer_the_same_way(seeded):
    """The UI keeps its own copy (it is an HTTP client and may not import the backend), so hold the two equal."""
    from lyst import answer_view as av
    from lyst.ui import components as ui

    lyst, owner = seeded
    results = [ask(lyst, owner, q)["result"] for q in ("Why did operating margin decline last quarter?",
                                                        "What was revenue last quarter by region?")]
    failed = {**results[0], "confidence": {"level": "unverified", "checks": [
        {"name": "recompute", "passed": False, "detail": "off by 2%"}, {"name": "coverage", "passed": True}]}}
    results += [failed, {**results[1], "status": "needs_input"}, {"answer": "x", "confidence": {"level": "verified"}}]
    for r in results:
        assert ui.trust_line(r) == av.trust_line(r)
        assert ui.caveats(r) == av.caveats(r)
        assert ui.breakdown_chart(r) == av.breakdown_chart(r)
    assert results[1].get("charts") or ui.breakdown_chart(results[1])  # the breakdown case is really exercised
    for col in ("prior_rate", "region", "operating_margin", "x"):
        assert ui.heading(col) == av.heading(col)
