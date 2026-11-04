# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
from lyst import evals


def test_demo_bank_passes(seeded):
    lyst, owner = seeded
    s = evals.run(lyst, owner, "northwind", evals.demo_bank(), label="ci")
    failed = [(r["case"], [c for c in r["checks"] if not c["passed"]]) for r in s["results"] if not r["passed"]]
    assert not failed, failed
    assert s["cases"] >= 15
    assert lyst.stores.control.scalar("SELECT passed FROM eval_runs WHERE id = ?", [s["run_id"]]) == s["cases"]


def test_harness_catches_wrong_answers(seeded):
    lyst, owner = seeded
    wrong = [evals.EvalCase("wrong-value", "What was revenue last quarter?", metric="revenue", value=1.0),
             evals.EvalCase("wrong-driver", "Why did operating margin decline last quarter?", driver="APAC"),
             evals.EvalCase("wrong-metric", "What was revenue last quarter?", metric="operating_margin")]
    s = evals.run(lyst, owner, "northwind", wrong)
    assert s["passed"] == 0
    failing = {r["case"]: {c["name"] for c in r["checks"] if not c["passed"]} for r in s["results"]}
    assert failing == {"wrong-value": {"value"}, "wrong-driver": {"driver"}, "wrong-metric": {"metric"}}
