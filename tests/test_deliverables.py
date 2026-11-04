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
    summary = {r[0].value: r[1].value for r in wb["Summary"].iter_rows(max_row=4)}
    assert summary["Answer"] == t["result"]["answer"] and summary["Confidence"] == "verified"
    table_sheet = next(n for n in wb.sheetnames if n.startswith("Change in Operating"))
    rows = list(wb[table_sheet].iter_rows(values_only=True))
    assert rows[0][0] == "region" and {r[0] for r in rows[1:]} == {"EMEA", "North America", "APAC", "LATAM"}
    m = lyst.artifacts.build(owner, t, "md")
    _, md = lyst.artifacts.get(owner, m["id"])
    text = md.decode()
    assert text.startswith("# Why did operating margin decline last quarter?")
    for heading in ("## Answer", "## Evidence", "## Business implications", "## Recommended actions", "## Supporting work"):
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
    assert "Answer" in text and "EMEA" in text and "Supporting work" in text and "sha256" in text
    assert "32.5%" in text
    d = lyst.artifacts.build(owner, t, "pptx")
    _, pptx = lyst.artifacts.get(owner, d["id"])
    prs = Presentation(io.BytesIO(pptx))
    titles = [s.shapes[0].text_frame.text for s in prs.slides]
    assert titles[1] == "Answer" and "Evidence" in titles and "Supporting work" in titles[-1]
    assert any("EMEA" in sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    with pytest.raises(ValueError):
        lyst.artifacts.build(owner, t, "docx")
    assert {a["kind"] for a in lyst.artifacts.list(owner, "northwind", t["id"])} == {"pdf", "pptx"}
