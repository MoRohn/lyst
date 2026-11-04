# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Living knowledge: automatic lineage, documents with citations, consistency with prior answers."""

import pytest

from lyst.knowledge import split_passages, tokens
from lyst.security import AccessDenied

from .conftest import ask

POLICY = b"""Revenue recognition policy

Revenue is recognised when control of goods transfers to the customer, which for hardware is shipment
and for services is the month of delivery. Intercompany sales between regions are excluded from
reported revenue.

Operating margin

Operating margin is operating income divided by revenue. The EMEA region reports in euros; amounts in
the management P&L are converted at the monthly average rate. A supplier price increase took effect for
EMEA hardware in July 2026.
"""


def test_lineage_is_recorded_as_work_happens(seeded):
    lyst, owner = seeded
    k = lyst.knowledge
    lin = k.lineage(owner, "northwind", "metric", "operating_margin")
    up = {(x["kind"], x["name"]) for x in lin["upstream"]}
    assert ("dataset", "pnl") in up and ("column", "pnl.operating_income") in up and ("column", "pnl.revenue") in up
    t = ask(lyst, owner, "What was operating margin last quarter?")
    lyst.artifacts.build(owner, t, "md")
    down = k.lineage(owner, "northwind", "metric", "operating_margin")["downstream"]
    kinds = {x["kind"] for x in down}
    assert "analysis" in kinds and "artifact" in kinds
    analysis = next(x for x in down if x["kind"] == "analysis")
    rel = k.relations(owner, "northwind", "analysis", analysis["name"])
    read = next(r for r in rel if r["rel"] == "read")
    assert read["to_name"] == "pnl" and read["evidence"]["version"] == 1 and len(read["evidence"]["sha256"]) == 64
    # the dataset's downstream reaches the artifact through metric and analysis
    ds_down = k.lineage(owner, "northwind", "dataset", "pnl")["downstream"]
    assert {x["kind"] for x in ds_down} >= {"metric", "analysis", "artifact", "column"}


def test_people_add_the_organizational_layer(seeded):
    lyst, owner = seeded
    k = lyst.knowledge
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        k.upsert_entity(viewer, "northwind", "team", "FP&A")
    k.upsert_entity(owner, "northwind", "team", "FP&A", description="Financial planning")
    k.relate(owner, "northwind", "team", "FP&A", "owns", "metric", "operating_margin", source="user")
    down = k.lineage(owner, "northwind", "team", "FP&A")["upstream"]
    assert ("metric", "operating_margin") in {(x["kind"], x["name"]) for x in down}
    assert k.lineage(viewer, "northwind", "metric", "operating_margin")  # viewers can read


def test_documents_are_searched_and_cited(seeded):
    lyst, owner = seeded
    k = lyst.knowledge
    assert len(split_passages(POLICY.decode())) == 2  # headings fold into their paragraphs
    assert "recognised" in tokens("Revenue is recognised when")
    d = k.add_document(owner, "northwind", POLICY, filename="policy.md", title="Finance policy")
    assert d["passages"] == 2
    hits = k.search(owner, "northwind", "why did operating margin decline in EMEA")
    assert hits and hits[0]["title"] == "Finance policy" and "supplier price increase" in hits[0]["text"]
    r = ask(lyst, owner, "Why did operating margin decline last quarter?")["result"]
    cites = r["supporting"]["citations"]
    assert cites and cites[0]["title"] == "Finance policy" and "EMEA" in cites[0]["quote"]
    # a document restricted to admins is invisible to a viewer
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    k.add_document(owner, "northwind", b"Board pack\n\nConfidential merger discussion about EMEA.", filename="board.txt",
                   min_role="admin")
    assert all(h["title"] != "board.txt" for h in k.search(viewer, "northwind", "merger EMEA"))
    assert any(h["title"] == "board.txt" for h in k.search(owner, "northwind", "merger EMEA"))
    assert {d["title"] for d in k.documents(viewer, "northwind")} == {"Finance policy"}


def test_pdf_documents(seeded, tmp_path):
    from pypdf import PdfWriter

    lyst, owner = seeded
    w = PdfWriter()
    w.add_blank_page(width=300, height=300)
    buf = tmp_path / "blank.pdf"
    with open(buf, "wb") as fh:
        w.write(fh)
    with pytest.raises(ValueError, match="no readable text"):
        lyst.knowledge.add_document(owner, "northwind", buf.read_bytes(), filename="blank.pdf")


def test_prior_answer_consistency_is_surfaced(seeded):
    lyst, owner = seeded
    first = ask(lyst, owner, "What was revenue last quarter?")["result"]
    second = ask(lyst, owner, "What was revenue last quarter?")["result"]
    cons = second["supporting"]["consistency"]
    assert cons and cons["agrees"] and cons["same_data"]
    # tamper with the stored earlier answer: the next run must flag the disagreement on the same data
    latest_task = lyst.stores.control.scalar("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 1")
    lyst.stores.workspace("northwind").execute(
        "UPDATE analyses SET answer = json_merge_patch(answer, '{\"answer\": \"Revenue was $1.00 in Q3 2026.\"}') "
        "WHERE task_id = ?", [latest_task])
    third = ask(lyst, owner, "What was revenue last quarter?")["result"]
    assert not third["supporting"]["consistency"]["agrees"]
    assert any("answered differently" in a for a in third["supporting"]["assumptions"])
    assert first["answer"] == third["answer"]


def test_validated_memory_reaches_the_answer(seeded):
    lyst, owner = seeded
    lyst.memory.remember(owner, "northwind", "team", "Operating margin targets exclude one-off restructuring costs.",
                         provenance={"kind": "correction"}, trust="user_validated")
    r = ask(lyst, owner, "What was operating margin last quarter?")["result"]
    assert any("Validated team knowledge: Operating margin targets" in a for a in r["supporting"]["assumptions"])
