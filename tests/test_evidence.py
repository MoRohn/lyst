# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Evidence graph and explanations (scenario C): a figure in an answer, and in a report built from it, traces to
the step that computed it, the SQL that ran, the governed definition version, the dataset version and its source.
Explanations come from those records, and a newer data version is reported instead of hidden."""

from __future__ import annotations

from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from tests.conftest import ask

WS = "northwind"


def _run(lyst, owner, q="What was revenue in 2024?"):
    t = ask(lyst, owner, q)
    assert t["status"] == "done", t
    return t


def test_figures_trace_to_steps_queries_definitions_and_sources(seeded):
    lyst, owner = seeded
    t = _run(lyst, owner)
    g = lyst.evidence.graph(owner, t["id"])
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"answer", "figure", "step", "query", "dataset_version", "source", "metric_version", "check"} <= kinds
    assert g["figures"]["traced"] >= 1
    rels = {(e["from"].split(":")[0], e["rel"], e["to"].split(":")[0]) for e in g["edges"]}
    assert {("figure", "computed_by", "step"), ("step", "ran", "query"), ("query", "read", "dataset"),
            ("dataset", "loaded_from", "source"), ("figure", "defined_by", "metric")} <= rels
    ds = next(n for n in g["nodes"] if n["id"] == "dataset:pnl@1")
    assert ds["sha256"] and ds["rows"] > 0 and ds["classification"] == "confidential"
    assert g["currency"] == {"current": True, "changed": []}

    how = lyst.evidence.explain(owner, t["id"], "how")
    assert "revenue definition v1" in how["short"] and "pnl v1" in how["short"]
    assert how["detail"][0]["queries"][0]["sql"].lower().startswith(("select", "with"))
    assert "pnl v1" in lyst.evidence.explain(owner, t["id"], "sources")["short"]
    assert lyst.evidence.explain(owner, t["id"], "method")["detail"]
    assert lyst.evidence.explain(owner, t["id"], "assumptions")["detail"]
    rep = lyst.evidence.explain(owner, t["id"], "reproduce")
    assert rep["reproducible"] is True and all(r["outcome"] == "same" for r in rep["detail"])


def test_a_newer_data_version_is_reported_not_hidden(seeded):
    lyst, owner = seeded
    t = _run(lyst, owner)
    frame = demo.pnl(seed=99)
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(frame), filename="pnl.csv", title="Monthly P&L", classification="confidential")
    g = lyst.evidence.graph(owner, t["id"])
    assert g["currency"]["current"] is False and g["currency"]["changed"][0] == {"dataset": "pnl", "used": 1, "current": 2}
    assert "newer version" in lyst.evidence.explain(owner, t["id"], "change")["short"]
    rep = lyst.evidence.reproduce(owner, t["id"])
    assert rep["reproducible"] is False and {r["outcome"] for r in rep["detail"]} == {"not_comparable"}


def test_report_figures_trace_through_the_artifact(seeded):
    lyst, owner = seeded
    t = _run(lyst, owner)
    art = lyst.artifacts.build(owner, t, "md")
    g = lyst.evidence.for_artifact(owner, art["id"])
    assert {"from": f"artifact:{art['id']}", "to": "answer", "rel": "presents"} in g["edges"]
    assert g["figures"]["traced"] >= 1


def test_evidence_follows_workspace_access_and_api(seeded):
    lyst, owner = seeded
    t = _run(lyst, owner)
    stranger = lyst.guard.create_user("x@elsewhere.test", "X")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    hs = {"Authorization": f"Bearer {lyst.guard.issue_token(stranger)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get(f"/v1/tasks/{t['id']}/evidence", headers=hs).status_code == 403
        g = c.get(f"/v1/tasks/{t['id']}/evidence", headers=h).json()
        assert g["figures"]["traced"] >= 1
        e = c.get(f"/v1/tasks/{t['id']}/explain", params={"q": "sources"}, headers=h).json()
        assert "pnl" in e["short"]
        assert c.get(f"/v1/tasks/{t['id']}/explain", params={"q": "astrology"}, headers=h).status_code == 400
        assert c.get(f"/v1/tasks/{t['id']}/explain", params={"q": "reproduce"}, headers=h).json()["reproducible"] is True
