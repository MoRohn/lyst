# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""New scenario E: a method Lyst lacks becomes a drafted, sandbox-tested, approved, reusable skill."""

import json

import httpx
import pandas as pd
import pytest

from lyst import demo
from lyst.analyst.foundry import TEMPLATES, detect_gap
from lyst.config import LLMSettings
from lyst.llm import LLMClient

from .conftest import ask


def test_gap_detection():
    assert detect_gap("What is the Herfindahl concentration of revenue by region?")["method"] == "herfindahl"
    assert detect_gap("compute the Gini of revenue across regions")["method"] == "gini"
    assert detect_gap("Run a kurtosis check on revenue across regions") == {"method": "kurtosis check", "template": False, "label": "kurtosis_check"}
    assert detect_gap("What was revenue last quarter?") is None


def test_e_gap_to_registered_skill(seeded):
    lyst, owner = seeded
    analyst = lyst.guard.create_user("ana@northwind.test", "Ana")
    lyst.guard.add_member(owner, "northwind", analyst.user_id, "editor")
    q = "What is the Herfindahl concentration of revenue by region last quarter?"
    first = ask(lyst, analyst, q)
    r = first["result"]
    assert r["status"] == "needs_input" and "drafted hhi_concentration v1" in r["answer"], r["answer"]
    assert "passed 2 representative and 3 adversarial cases" in r["answer"]
    assert "No number was produced" in r["supporting"]["assumptions"][-1]
    proposal = next(json.loads(s) for s in [lyst.stores.control.scalar(
        "SELECT output FROM task_steps WHERE task_id = ? AND capability = 'propose_capability'", [first["id"]])])
    assert proposal["drafted_by"] == "template" and proposal["benchmark_ms"] >= 0
    skill = lyst.skills.get(owner, "northwind", "hhi_concentration")
    assert skill["status"] == "tested" and skill["proposed_by"] == analyst.user_id
    # asking again does not re-draft; it points at the pending approval
    again = ask(lyst, analyst, q)["result"]
    assert "already passed its tests" in again["answer"]
    assert len([s for s in lyst.skills.list(owner, "northwind") if s["name"] == "hhi_concentration"]) == 1
    # the proposer cannot approve; the owner can
    pending = lyst.runtime.approvals(owner, "northwind")
    ap = next(a for a in pending if a["action"] == "skill.register")
    from lyst.security import AccessDenied

    with pytest.raises(AccessDenied):
        lyst.runtime.decide(analyst, ap["id"], True)
    lyst.runtime.decide(owner, ap["id"], True)
    # now the same question runs the skill on governed data, and the number is right
    done = ask(lyst, analyst, q)
    r = done["result"]
    assert done["status"] == "done" and r["status"] == "complete"
    df = demo.pnl()
    m = pd.to_datetime(df["month"])
    q3 = df[(m >= "2026-07-01") & (m < "2026-10-01")].groupby("region").revenue.sum()
    hhi = float(((q3 / q3.sum()) ** 2).sum())
    assert f"HHI is {hhi:.3f}" in r["answer"]
    assert r["supporting"]["queries"][0]["purpose"].startswith("input to skill hhi_concentration v1")
    assert "WHERE" in r["supporting"]["queries"][0]["sql"]  # the quarter filter made it into the input query


def test_viewer_gets_a_blocker_not_a_draft(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("v@n.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    r = ask(lyst, viewer, "What is the Gini of revenue across regions?")["result"]
    assert r["status"] == "needs_input" and "cannot propose" in r["answer"]
    assert not [s for s in lyst.skills.list(owner, "northwind") if s["name"] == "gini_inequality"]


def test_unknown_method_without_a_model_reports_the_contract(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Run a kurtosis check on revenue across regions")["result"]
    assert r["status"] == "needs_input" and "no model is configured to draft one" in r["answer"]


def test_model_drafted_skill_must_pass_the_sandbox(seeded):
    """A model draft that passes its tests is proposed; one that writes bad code is refused."""
    lyst, owner = seeded
    good = {"source": "def run(df, **params):\n    import math\n    v = [float(x) for x in df['value'].dropna()]\n"
                      "    s = sum(v)\n    return {'summary': f'Entropy over {len(v)} members.', 'entropy': "
                      "float(-sum((x/s)*math.log(x/s) for x in v if x > 0)) if s else None, 'members': len(v)}",
            "tests": [{"name": "two equal", "columns": ["member", "value"], "rows": [["A", 1.0], ["B", 1.0]],
                       "expect": {"summary": "Entropy over 2 members.", "entropy": 0.6931471805599453, "members": 2}, "tolerance": 1e-9}],
            "triggers": ["entropy"]}
    bad = {**good, "source": "import os\n" + good["source"]}
    answers = iter([good, bad])

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(next(answers))}}], "usage": {}})

    lyst.services.llm = LLMClient(LLMSettings("http://localhost:11434/v1", "local"), lyst.stores, transport=httpx.MockTransport(handler))
    r = ask(lyst, owner, "Compute the entropy of revenue across regions")["result"]
    assert "drafted entropy v1 (model)" in r["answer"], r["answer"]
    r = ask(lyst, owner, "Compute the skewness of revenue across regions")["result"]
    assert "failed the static check" in r["answer"] and "import of os" in r["answer"]


def test_templates_have_exact_tests():
    from lyst.skills import run_sandboxed

    for key, t in TEMPLATES.items():
        rep = run_sandboxed(t["source"], t["tests"])
        assert not rep.get("error") and all(c["passed"] for c in rep["cases"]), (key, rep)
