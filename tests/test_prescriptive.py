# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario C (forecast + uncertainty + staffing scenarios), event impact with stated assumptions, member filters."""

import numpy as np
import pandas as pd
import pytest

from lyst import demo
from lyst.analytics import causal, planning

from .conftest import ask


def test_c_forecast_next_quarter_and_three_staffing_scenarios(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Forecast next quarter's support demand, quantify uncertainty, and evaluate three staffing scenarios.")
    assert t["status"] == "done", t["error"]
    r = t["result"]
    assert "Q4 2026" in r["answer"] and "80% interval" in r["answer"]
    fp = next(x for x in r["tables"] if x["title"].startswith("Monthly forecast"))
    assert [row[0] for row in fp["rows"]] == ["2026-10-01", "2026-11-01", "2026-12-01"]
    sc = next(x for x in r["tables"] if x["title"].startswith("Staffing scenarios"))
    assert len(sc["rows"]) == 3
    p_short = [row[3] for row in sc["rows"]]
    assert p_short[0] > p_short[1] > p_short[2]  # more agents, less risk
    assert any("backtest error" in e for e in r["evidence"])
    assert r["actions"][0].startswith("Plan for +20%")
    assert any("Staffing parameters come from staffing" in a for a in r["supporting"]["assumptions"])
    checks = {c["name"]: c["passed"] for c in r["confidence"]["checks"]}
    assert checks["scenario_capacity"] and checks["forecast_interval"]
    # the quarter total is the sum of the monthly point forecasts
    total = float(r["answer"].split(" of ")[1].split(" for ")[0].replace(",", ""))
    assert total == pytest.approx(sum(row[1] for row in fp["rows"]), abs=1)


def test_named_scenarios_are_used(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "Forecast next quarter's ticket volume and compare staffing scenarios with 45, 50 and 55 agents")["result"]
    sc = next(x for x in r["tables"] if x["title"].startswith("Staffing scenarios"))
    assert [row[1] for row in sc["rows"]] == [45, 50, 55]


def test_event_impact_is_a_hedged_causal_estimate(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was the impact of the supplier price increase on operating margin in EMEA since July 2026?")["result"]
    assert r["answer"].startswith("Since 2026-07") and "plausible causal estimate" in r["answer"]
    effect = float(r["answer"].split("averaged ")[1].split(" pts")[0])
    assert -13.5 < effect < -11.0  # the planted 12-point COGS jump
    assert any("Identification (interrupted time series" in a for a in r["supporting"]["assumptions"])
    assert any(a == "Filtered to region = EMEA." for a in r["supporting"]["assumptions"])
    # no effect where nothing happened
    r2 = ask(lyst, owner, "What was the effect of the new pricing on revenue in APAC since March 2025?")["result"]
    assert "not statistically distinguishable" in r2["answer"]


def test_member_filters_apply_everywhere(seeded):
    lyst, owner = seeded
    r = ask(lyst, owner, "What was revenue in EMEA last quarter?")["result"]
    df = demo.pnl()
    m = pd.to_datetime(df["month"])
    emea = df[(m >= "2026-07-01") & (m < "2026-10-01") & (df.region == "EMEA")].revenue.sum()
    assert f"${emea / 1e6:.2f}M" in r["answer"]
    r = ask(lyst, owner, "Why did operating margin decline in EMEA last quarter?")["result"]
    assert "product line" in r["evidence"][0] and "region" not in r["evidence"][0]  # region is fixed, so not a driver


def test_interrupted_time_series_recovers_a_step():
    rng = np.random.default_rng(1)
    t = np.arange(30)
    y = pd.Series(10 + 0.1 * t + np.where(t >= 20, -3.0, 0.0) + rng.normal(0, 0.2, 30))
    eff = causal.interrupted_time_series(y, 20)
    assert -3.6 < eff.effect < -2.4 and eff.ci_low < eff.effect < eff.ci_high and eff.p_value < 0.01
    assert eff.details["level_change"] == pytest.approx(-3.0, abs=0.5)
    flat = causal.interrupted_time_series(pd.Series(10 + 0.1 * t + rng.normal(0, 0.2, 30)), 20)
    assert flat.p_value > 0.05
    with pytest.raises(ValueError):
        causal.interrupted_time_series(pd.Series([1.0, 2.0, 3.0]), 1)


def test_difference_in_differences():
    rng = np.random.default_rng(2)
    t = np.arange(16)
    control = pd.Series(50 + 0.5 * t + rng.normal(0, 0.3, 16))
    treated = pd.Series(55 + 0.5 * t + np.where(t >= 10, 4.0, 0.0) + rng.normal(0, 0.3, 16))
    eff = causal.difference_in_differences(treated, control, 10)
    assert 3.0 < eff.effect < 5.0 and eff.p_value < 0.01 and not eff.warnings
    bad = causal.difference_in_differences(pd.Series(50 + 3.0 * t), control, 10)
    assert bad.warnings and "parallel trends" in bad.warnings[0]


def test_planning_helpers():
    q = planning.quarter_from_monthly([100, 100, 100], [90, 90, 90], [110, 110, 110])
    assert q["total"] == 300 and q["lo"] < 300 < q["hi"] and q["hi"] - 300 < 30  # tighter than summing intervals
    res = planning.staffing_scenarios(1000, 50, [{"name": "a", "agents": 10, "capacity_per_agent": 90},
                                                 {"name": "b", "agents": 10, "capacity_per_agent": 120}])
    assert res[0].p_shortfall > 0.9 and res[1].p_shortfall < 0.01
    assert planning.minimum_staff(1000, 50, 100) == 11
    plan = planning.allocate([{"name": "email", "demand": 900, "capacity_per_agent": 100, "cost_per_agent": 1},
                              {"name": "phone", "demand": 300, "capacity_per_agent": 50, "cost_per_agent": 2}], safety=1.0)
    assert plan["total_agents"] == 15 and plan["total_cost"] == 21
