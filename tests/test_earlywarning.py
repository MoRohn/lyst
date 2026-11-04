# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Early warning. Acceptance scenario B: a monitored KPI starts deteriorating before it crosses the alert line; Lyst
verifies the signal, investigates it read-only, and surfaces one concise finding. False positives (noise, seasonality,
an investigation that cannot attribute the move) stay silent."""

import json

import numpy as np
import pandas as pd
import pytest

from lyst import demo
from lyst.anticipate import earlywarning
from lyst.semantic import Measure, MetricSpec

WS = "northwind"
MONTHS = pd.date_range("2024-04-01", "2026-09-01", freq="MS")


def _series(values) -> pd.Series:
    return pd.Series(values, index=MONTHS[-len(values):])


def test_assess_finds_a_developing_decline():
    rng = np.random.default_rng(1)
    v = 1000 + rng.normal(0, 8, 30)
    v[-4:] -= [20, 40, 60, 80]
    a = earlywarning.assess(_series(v), "month", 3.0, True)
    names = {s["signal"] for s in a["signals"]}
    assert a["raise"] and "sustained_trend" in names and len(a["families"]) >= 2, a["signals"]


def test_assess_is_quiet_on_noise_and_a_single_blip():
    rng = np.random.default_rng(2)
    v = 1000 + rng.normal(0, 8, 30)
    v[-1] -= 20
    a = earlywarning.assess(_series(v), "month", 3.0, True)
    assert not a["raise"], a["signals"]


def test_assess_marks_a_seasonal_dip():
    rng = np.random.default_rng(3)
    base = np.array([0, 0, 0, 0, 0, 0, 0, 0, -20, -40, -60, -80], dtype=float)   # every Jun-Sep falls
    v = 1000 + np.tile(np.roll(base, -3), 3)[:30] + rng.normal(0, 4, 30)
    a = earlywarning.assess(_series(v), "month", 3.0, True)
    assert a["seasonal"]


def _plant(decline: bool) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    rows = []
    for i, month in enumerate(MONTHS):
        for site in ("Plant A", "Plant B", "Plant C"):
            for line in ("Line 1", "Line 2"):
                units = 500 + rng.normal(0, 4)
                late = i - (len(MONTHS) - 4)
                if decline and site == "Plant B" and late >= 0:
                    units -= 18 * (late + 1)
                rows.append({"month": month.date().isoformat(), "site": site, "line": line, "units": round(units, 1)})
    return pd.DataFrame(rows)


@pytest.fixture
def plant(seeded, request):
    lyst, owner = seeded
    lyst.data.ingest(owner, WS, "output", demo.csv_bytes(_plant(getattr(request, "param", True))), filename="output.csv",
                     title="Plant output")
    lyst.semantic.define(owner, WS, MetricSpec(name="throughput", title="Throughput", dataset="output",
                                               measure=Measure(agg="sum", column="units"), time_column="month",
                                               dimensions=["site", "line"], synonyms=["output", "units produced"]))
    m = lyst.monitors.create(owner, WS, metric="throughput", grain="month", threshold_sigma=4.0)
    lyst.runtime.decide(owner, m["approval_id"], True)
    return lyst, owner


def test_b_emerging_risk_is_investigated_before_anyone_is_told(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="early_warning")
    assert a["status"] == "preparing" and a["task_id"] and a["finding_id"] is None     # nothing said yet
    t = lyst.stores.control.query("SELECT kind, priority, deadline_at, created_by, autonomy FROM tasks WHERE id = ?", [a["task_id"]])[0]
    assert t["kind"] == "investigation" and t["priority"] == 3 and t["deadline_at"] is not None and t["created_by"] == owner.user_id
    lyst.runtime.drain()
    out = lyst.anticipation.cycle(WS)
    assert out["surfaced"] == 1, out
    full = lyst.anticipation.get(owner, WS, a["id"])
    assert full["verdict"] == "verified" and full["status"] == "surfaced"
    finding = full["prepared"]["finding"]
    assert any("Plant B" in d for d in finding["drivers"]), finding
    why = lyst.anticipation.explain(owner, WS, a["id"])
    assert "deteriorating" in why["short"] and any("interval" in d for d in why["detail"])
    inbox = [i for i in lyst.monitors.inbox(owner, WS) if i["id"] == full["finding_id"]]
    assert inbox and "Plant B" in inbox[0]["summary"]
    assert lyst.anticipation.cycle(WS)["surfaced"] == 0                                # once
    assert lyst.anticipation.settings(owner, WS)["usage_today"]["investigate"]["count"] == 1


def test_b_at_prepare_level_the_warning_waits_unverified(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    out = lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="early_warning")
    assert a["verdict"] == "unverified" and a["task_id"] is None and out["surfaced"] == 0
    assert lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE kind = 'investigation'") == 0


@pytest.mark.parametrize("plant", [False], indirect=True)
def test_b_steady_metric_raises_nothing(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    assert lyst.anticipation.list(owner, WS, kind="early_warning") == []
    assert lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE kind = 'investigation'") == 0


def test_b_an_investigation_that_cannot_attribute_the_move_closes_silently(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="early_warning")
    lyst.stores.control.execute("UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
                                [json.dumps({"answer": "Throughput fell.", "evidence": [], "confidence": {"level": "not verified"}}),
                                 a["task_id"]])
    lyst.anticipation.cycle(WS)
    assert lyst.stores.workspace(WS).scalar("SELECT status FROM anticipations WHERE id = ?", [a["id"]]) == "suppressed"
    assert not [i for i in lyst.monitors.inbox(owner, WS) if (i["monitor_id"] or "").startswith("anticipate:")]


def test_b_investigation_budget_holds_the_warning_unverified(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 3, "daily_investigation_limit": 0})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="early_warning")
    assert a["verdict"] == "unverified" and "budget" in a["reasons"][-1]


def test_lowering_the_level_cancels_a_queued_investigation(plant):
    lyst, owner = plant
    lyst.anticipation.set_settings(owner, WS, {"level": 3})
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS, kind="early_warning")
    lyst.anticipation.set_settings(owner, WS, {"level": 2})
    assert lyst.stores.control.scalar("SELECT status FROM tasks WHERE id = ?", [a["task_id"]]) == "cancelled"
    assert lyst.anticipation.list(owner, WS, kind="early_warning") == []
