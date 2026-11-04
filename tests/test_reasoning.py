# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario A (deep): competing hypotheses decided by computation, horizon and change points, related metrics."""

import pandas as pd
import pytest

from lyst import demo
from lyst.analyst.reasoning import depth_of, horizon_of
from lyst.analytics.changepoint import change_points

from .conftest import ask

DEEP = ("Investigate our declining operating margins across the last six quarters, isolate the strongest "
        "contributing factors, test alternative explanations, and prepare an executive briefing.")


def test_depth_and_horizon_parsing():
    assert horizon_of(DEEP) == (6, "quarter")
    assert horizon_of("revenue over the last 12 months") == (12, "month")
    assert depth_of("What was revenue last quarter?", "value", None) == "fast"
    assert depth_of("Why did margin fall?", "explain", None) == "analytical"
    assert depth_of(DEEP, "explain", (6, "quarter")) == "deep"
    assert depth_of("Why did margin fall? Test alternative explanations.", "explain", None) == "deep"
    assert depth_of("Build a procurement reporting dashboard", "value", None) == "project"


def test_change_points_find_a_step_and_stay_quiet_on_noise():
    step = [1.0, 1.02, 0.98, 1.01, 0.70, 0.72, 0.69, 0.71]
    b = change_points(step)
    assert len(b) == 1 and b[0].index == 4 and b[0].shift == pytest.approx(-0.3, abs=0.03)
    assert change_points([1.0, 1.01, 0.99, 1.02, 0.98, 1.0, 1.01, 0.99]) == []
    assert change_points([1, 2, 3]) == []
    two = [1.0] * 4 + [2.0] * 4 + [3.0] * 4
    assert [x.index for x in change_points(two)] == [4, 8]


def test_a_deep_investigation_tests_competing_explanations(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, DEEP)
    assert t["status"] == "done", t["error"]
    caps = [s["capability"] for s in t["steps"]]
    for c in ("horizon_series", "change_points", "hypothesize", "test_hypotheses", "related_metrics"):
        assert c in caps
    r = t["result"]
    assert r["depth"] == "deep" and r["status"] == "complete"
    v = {h["id"]: h for h in r["hypotheses"]}
    assert set(v) == {"concentration", "broad", "mix", "seasonality", "single_month", "artifact", "parts"}
    assert v["concentration"]["verdict"] == "supported" and v["concentration"]["numbers"]["member"] == "EMEA"
    assert v["artifact"]["verdict"] == "refuted"
    assert v["seasonality"]["verdict"] == "refuted"      # the planted jump did not happen a year earlier
    assert v["parts"]["verdict"] == "supported" and v["parts"]["numbers"]["mover"] == "numerator"
    assert v["mix"]["verdict"] == "refuted"
    # the numbers behind "parts" are exact: operating income fell 8.7%, revenue rose 1.3% (pandas check)
    df = demo.pnl()
    m = pd.to_datetime(df["month"])
    q3, q2 = df[(m >= "2026-07-01") & (m < "2026-10-01")], df[(m >= "2026-04-01") & (m < "2026-07-01")]
    assert v["parts"]["numbers"]["numerator_change"] == pytest.approx(
        q3.operating_income.sum() / q2.operating_income.sum() - 1, rel=1e-9)
    assert v["parts"]["numbers"]["denominator_change"] == pytest.approx(q3.revenue.sum() / q2.revenue.sum() - 1, rel=1e-9)
    # the decision is explicit, and established findings come before hypotheses
    assert any("favours concentration in one segment" in e for e in r["evidence"])
    assert "confined to Q3 2026" in r["evidence"][0]
    assert any("Related metrics" in e and "Operating income down" in e for e in r["evidence"])
    assert any("not the usual seasonal pattern" in i for i in r["implications"])
    checks = {c["name"]: c["passed"] for c in r["confidence"]["checks"]}
    assert checks["hypotheses_decided"] and checks["parts_reconcile"] and r["confidence"]["level"] == "verified"
    assert r["charts"][0]["type"] == "bar" and len(r["charts"][0]["x"]) == 6
    src = r["supporting"]["sources"][0]
    assert src["dataset"] == "pnl" and len(src["sha256"]) == 64 and src["data_through"] == "2026-09-30"
    assert r["supporting"]["generated_at"]


def test_data_artifact_hypothesis_fires_on_the_ledger(seeded):
    """The ledger copy has a duplicated row and a null revenue; an investigation on it must say so."""
    lyst, owner = seeded
    from lyst.semantic import Measure, MetricSpec

    lyst.semantic.define(owner, "northwind", MetricSpec(
        name="ledger_margin", title="Ledger operating margin", dataset="pnl_ledger", kind="ratio",
        numerator=Measure(agg="sum", column="operating_income"), denominator=Measure(agg="sum", column="revenue"),
        time_column="month", dimensions=["region", "product_line"], format="percent"))
    # the ledger's planted problems are in Mar 2026 (dup) and Aug 2026 (null revenue): investigate Q3 2026
    t = ask(lyst, owner, "Investigate why ledger operating margin changed last quarter and test alternative explanations")
    r = t["result"]
    v = {h["id"]: h for h in r["hypotheses"]}
    assert v["artifact"]["verdict"] == "supported" and "nulls in revenue" in v["artifact"]["detail"]
    assert r["implications"][0].startswith("Data problems were found inside the period")
    assert r["confidence"]["level"] == "verified with warnings"
