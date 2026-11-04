# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Experiment registry: method comparison with honest verdicts, recorded inputs, reproduction and decisions."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.experiments import backtest, compare_methods
from lyst.security import AccessDenied


def _series(n=48, seed=3, season=True):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    y = 100 + 1.5 * t + (12 * np.sin(2 * np.pi * t / 12) if season else 0) + rng.normal(0, 2, n)
    return pd.Series(y, index=pd.date_range("2022-01-01", periods=n, freq="MS"))


def test_backtest_scores_and_a_clear_winner_on_seasonal_data():
    y = _series()
    runs = []
    for i, m in enumerate(["naive", "mean", "seasonal_naive", "ets"]):
        out = backtest(y, m, horizon=3, folds=6, sp=12)
        assert out["metrics"]["folds"] == 6 and out["metrics"]["points"] == 18
        runs.append({"id": f"r{i}", "method": m, "status": "ok", "metrics": out["metrics"], "detail": out})
    cmp = compare_methods(runs)
    assert cmp["ranking"][0]["method"] == "ets" and cmp["clear"] and cmp["winner"] == "ets"
    assert cmp["ranking"][-1]["method"] == "mean"
    with pytest.raises(ValueError, match="needs at least"):
        backtest(y.iloc[:10], "ets", horizon=3, folds=2, sp=12)


def test_noise_has_no_winner_and_the_simpler_method_is_recommended():
    rng = np.random.default_rng(1)
    y = pd.Series(50 + rng.normal(0, 5, 40), index=pd.date_range("2023-01-01", periods=40, freq="MS"))
    runs = []
    for i, m in enumerate(["naive", "drift"]):
        out = backtest(y, m, horizon=2, folds=4, sp=12)
        runs.append({"id": f"r{i}", "method": m, "status": "ok", "metrics": out["metrics"], "detail": out})
    cmp = compare_methods(runs)
    assert not cmp["clear"] and cmp["winner"] == "naive" and "no clear winner" in cmp["verdict"]


def test_forecast_experiment_end_to_end(seeded):
    lyst, owner = seeded
    ex = lyst.experiments
    e = ex.create(owner, "northwind", name="Revenue forecast method", kind="forecast",
                  question="Which method should we trust for monthly revenue?",
                  spec={"metric": "revenue", "grain": "month", "horizon": 3, "folds": 4,
                        "methods": ["naive", "seasonal_naive", "drift", "ets"]})
    out = ex.run(owner, "northwind", e["id"])
    assert len(out["ranking"]) == 4 and out["winner"] in {"naive", "seasonal_naive", "drift", "ets"}
    assert out["inputs"]["metric"] == "revenue" and out["inputs"]["datasets"] == {"pnl": 1}
    assert out["default"] == "ets" and out["default_rank"] in (1, 2, 3, 4)
    run = ex.get(owner, "northwind", e["id"])["runs"][0]
    assert run["environment"]["packages"]["numpy"] and run["input_digest"] and run["result_digest"]
    assert len(run["inputs"]["series"]["values"]) == out["points"]
    # reproduce on the recorded series: same scores; then change the data and see observed_changed
    rep = ex.reproduce(owner, "northwind", run["id"])
    assert rep["same"] is True and rep["differences"] == [] and rep["observed_changed"] is False and rep["environment"] == []
    frame = lyst.data.query(owner, "northwind", "SELECT * FROM pnl").frame()
    frame.loc[0, "revenue"] = frame.loc[0, "revenue"] * 3
    lyst.data.ingest(owner, "northwind", "pnl", frame.to_csv(index=False).encode(), filename="pnl.csv")
    rep = ex.reproduce(owner, "northwind", run["id"])
    assert rep["same"] is True and rep["observed_changed"] is True
    # deciding against the evidence is allowed and recorded as such
    other = next(r["method"] for r in out["ranking"] if r["method"] != out["winner"])
    d = ex.decide(owner, "northwind", e["id"], choice=other, note="the planning team wants it")
    assert d["status"] == "decided" and d["decision"]["against_evidence"] is True
    with pytest.raises(ValueError, match="decided"):
        ex.run(owner, "northwind", e["id"])
    assert ex.list(owner, "northwind")[0]["runs"] >= 6


def test_experiment_validation_and_permissions(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.experiments.create(viewer, "northwind", name="x", kind="forecast", spec={"metric": "revenue"})
    with pytest.raises(ValueError, match="unknown method"):
        lyst.experiments.create(owner, "northwind", name="x", kind="forecast", spec={"metric": "revenue", "methods": ["prophet"]})
    with pytest.raises(KeyError):
        lyst.experiments.create(owner, "northwind", name="x", kind="forecast", spec={"metric": "nope"})
    with pytest.raises(ValueError, match="kind"):
        lyst.experiments.create(owner, "northwind", name="x", kind="ab", spec={})


def test_eval_experiment_compares_runs(seeded):
    lyst, owner = seeded
    e = lyst.experiments.create(owner, "northwind", name="Eval gate", kind="eval",
                                spec={"cases": ["revenue-last-quarter"], "label": "before"})
    first = lyst.experiments.run(owner, "northwind", e["id"])
    second = lyst.experiments.run(owner, "northwind", e["id"])
    assert second["gate"]["ok"] is True and second["verdict"] == "no regressions"
    assert first["baseline"] == second["baseline"]


def test_experiment_api(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert "ets" in c.get("/v1/experiments/methods", headers=h).json()["forecast"]
        r = c.post("/v1/workspaces/northwind/experiments", headers=h, json={
            "name": "Margin", "spec": {"metric": "operating_income", "methods": ["naive", "ets"], "folds": 3}})
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        out = c.post(f"/v1/workspaces/northwind/experiments/{eid}/run", headers=h).json()
        assert {x["method"] for x in out["ranking"]} == {"naive", "ets"}
        assert c.get(f"/v1/workspaces/northwind/experiments/{eid}/comparison", headers=h).json()["batch"] == out["batch"]
        rid = out["ranking"][0]["run_id"]
        assert c.post(f"/v1/workspaces/northwind/experiment-runs/{rid}/reproduce", headers=h).json()["same"] is True
        r = c.post(f"/v1/workspaces/northwind/experiments/{eid}/decision", headers=h, json={"choice": "arima"})
        assert r.status_code == 400
