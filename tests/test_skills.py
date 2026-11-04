# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario F: a repeatable task Lyst cannot do yet becomes a governed, tested, approved skill."""

import pytest

from lyst.security import AccessDenied
from lyst.skills import SkillRejected, run_sandboxed, static_check

from .conftest import ask

INPUT = ("SELECT region, sum(revenue) AS revenue FROM pnl WHERE month >= DATE '2026-07-01' "
         "AND month < DATE '2026-10-01' GROUP BY region")

BUGGY = '''
def run(df, top=1):
    total = df["revenue"].sum()
    ranked = df.sort_values("revenue", ascending=False)
    share = ranked["revenue"].head(top).sum() / total
    return {"summary": f"The top {top} region(s) earn {share:.1%} of revenue.", "share": float(share)}
'''

FIXED = '''
import math

def run(df, top=1):
    if df.empty or df["revenue"].isna().all():
        return {"summary": "No revenue in the input.", "share": None, "table": []}
    df = df.dropna(subset=["revenue"])
    total = float(df["revenue"].sum())
    ranked = df.sort_values("revenue", ascending=False)
    share = float(ranked["revenue"].head(top).sum()) / total if total else math.nan
    table = [{"region": r, "revenue": float(v), "share": float(v) / total} for r, v in zip(ranked["region"], ranked["revenue"])]
    return {"summary": f"The top {top} region(s) earn {share:.1%} of revenue last quarter.",
            "share": share, "table": table, "findings": [f"{table[0]['region']} leads with {table[0]['share']:.1%}."]}
'''

TESTS = [{"name": "two regions", "columns": ["region", "revenue"], "rows": [["A", 75.0], ["B", 25.0]],
          "params": {"top": 1}, "expect": {"summary": "The top 1 region(s) earn 75.0% of revenue last quarter.",
                                           "share": 0.75, "table": [{"region": "A", "revenue": 75.0, "share": 0.75},
                                                                    {"region": "B", "revenue": 25.0, "share": 0.25}],
                                           "findings": ["A leads with 75.0%."]}}]


def test_static_check_refuses_dangerous_code():
    assert "import of os is not allowed" in static_check("import os\ndef run(df): return {}")
    assert "use of open is not allowed" in static_check("def run(df): return {'x': open('/etc/passwd').read()}")
    assert any("read_csv" in p for p in static_check("import pandas as pd\ndef run(df): return pd.read_csv('/x')"))
    assert any("__class__" in p for p in static_check("def run(df): return ().__class__.__bases__"))
    assert static_check("def helper(): pass") == ["define run(df, **params)"]


def test_sandbox_blocks_imports_and_files_at_runtime():
    # bypasses the static check on purpose, to prove the runtime layer holds on its own
    rep = run_sandboxed("def run(df):\n    import socket\n    return {}", [{"columns": [], "rows": []}])
    assert "not allowed" in rep["cases"][0]["error"]
    rep = run_sandboxed("def run(df):\n    return {'x': open('/etc/hosts').read()}", [{"columns": [], "rows": []}])
    assert "NameError" in rep["cases"][0]["error"]
    rep = run_sandboxed("def run(df):\n    while True: pass", [{"columns": [], "rows": []}], timeout_s=3, cpu_seconds=2)
    assert rep.get("error") or "error" in rep["cases"][0]


def test_f_propose_test_approve_register_and_use(seeded):
    lyst, owner = seeded
    sk = lyst.skills
    analyst = lyst.guard.create_user("ana@northwind.test", "Ana")
    lyst.guard.add_member(owner, "northwind", analyst.user_id, "editor")

    # before: Lyst answers with revenue by region, but has no concentration measure
    before = ask(lyst, analyst, "What is our revenue concentration by region?")["result"]
    assert not any("share" in t["columns"] for t in before["tables"])

    # v1 has a bug the adversarial cases find (division by zero on empty input)
    v1 = sk.propose(analyst, "northwind", name="revenue_concentration", purpose="Share of revenue from the top regions",
                    source=BUGGY, input_sql=INPUT, tests=[{**TESTS[0], "expect": {
                        "summary": "The top 1 region(s) earn 75.0% of revenue.", "share": 0.75}}],
                    triggers=["revenue concentration"])
    r1 = sk.test(analyst, "northwind", "revenue_concentration", v1["version"])
    assert r1["status"] == "failed"
    failed = [c["name"] for c in r1["report"]["cases"] if not c["passed"]]
    assert "empty input" in failed
    with pytest.raises(SkillRejected):
        sk.request_registration(analyst, "northwind", "revenue_concentration", v1["version"], lyst.runtime)

    # v2 fixes it
    v2 = sk.propose(analyst, "northwind", name="revenue_concentration", purpose="Share of revenue from the top regions",
                    source=FIXED, input_sql=INPUT, tests=TESTS, triggers=["revenue concentration"])
    r2 = sk.test(analyst, "northwind", "revenue_concentration", v2["version"])
    assert r2["status"] == "tested", r2["report"]
    assert r2["report"]["representative"] == 1 and r2["report"]["adversarial"] == 3

    ap = sk.request_registration(analyst, "northwind", "revenue_concentration", v2["version"], lyst.runtime)
    with pytest.raises(AccessDenied):  # an editor cannot approve
        lyst.runtime.decide(analyst, ap, True)
    lyst.runtime.decide(owner, ap, True)
    assert sk.get(owner, "northwind", "revenue_concentration", v2["version"])["status"] == "approved"
    assert sk.get(owner, "northwind", "revenue_concentration", v2["version"])["approved_by"] == owner.user_id

    # after: the analyst plans with it, on governed data, with evidence
    t = ask(lyst, analyst, "What is our revenue concentration by region?")
    r = t["result"]
    assert t["status"] == "done" and r["status"] == "complete", t
    assert "EMEA" not in r["answer"] and "of revenue last quarter" in r["answer"]
    assert r["tables"][0]["columns"] == ["region", "revenue", "share"]
    assert r["supporting"]["queries"][0]["purpose"].startswith("input to skill revenue_concentration v2")
    assert abs(sum(row[2] for row in r["tables"][0]["rows"]) - 1) < 1e-9

    # retiring it takes it out of plans
    sk.retire(owner, "northwind", "revenue_concentration")
    after = ask(lyst, analyst, "What is our revenue concentration by region?")["result"]
    assert not any("share" in t["columns"] for t in after["tables"])


def test_proposer_who_is_owner_still_cannot_self_approve(seeded):
    lyst, owner = seeded
    v = lyst.skills.propose(owner, "northwind", name="row_count", purpose="Rows", source="def run(df):\n    return {'n': len(df)}",
                            input_sql="SELECT region FROM pnl", tests=[{"columns": ["region"], "rows": [["A"]], "expect": {"n": 1}}],
                            triggers=["row count"])
    assert lyst.skills.test(owner, "northwind", "row_count", v["version"])["status"] == "tested"
    ap = lyst.skills.request_registration(owner, "northwind", "row_count", v["version"], lyst.runtime)
    with pytest.raises(AccessDenied):
        lyst.runtime.decide(owner, ap, True)


def test_rejected_source_is_stored_but_cannot_run(seeded):
    lyst, owner = seeded
    v = lyst.skills.propose(owner, "northwind", name="leaky", purpose="x", source="import os\ndef run(df): return {}",
                            input_sql="SELECT region FROM pnl", tests=[{"rows": [], "expect": {}}], triggers=["leaky"])
    assert v["status"] == "rejected" and "import of os is not allowed" in v["problems"]
    with pytest.raises(SkillRejected):
        lyst.skills.test(owner, "northwind", "leaky", v["version"])
