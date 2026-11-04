# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import io

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
