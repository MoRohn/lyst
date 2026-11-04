# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Cost and value attribution (scenario J): several analyses run, model calls are attributed to the people, tasks
and models that made them, retries are counted as overhead, value is counted from records with no invented ROI,
and recommendations cite the numbers behind them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.security import AccessDenied
from tests.conftest import ask

WS = "northwind"


def _calls(lyst, task_id, model, n, *, failed=0, cost=0.002):
    for i in range(n):
        lyst.stores.control.execute(
            "INSERT INTO llm_calls (id, workspace_id, task_id, model, purpose, external, input_tokens, output_tokens, latency_ms, outcome, "
            "attempt, cost_usd, workload) VALUES (nextval('llm_call_seq'), ?, ?, ?, 'deliver', true, 400, 80, 900, ?, ?, ?, 'chat')",
            [WS, task_id, model, "error" if i < failed else "ok", 2 if i < failed else 1, cost])


def test_scenario_j_attribution_value_and_grounded_recommendations(seeded):
    lyst, owner = seeded
    analyst = lyst.guard.create_user("ana@northwind.test", "Ana")
    lyst.guard.add_member(owner, WS, analyst.user_id, "editor")
    tasks = [ask(lyst, owner, "What was revenue in 2024?") for _ in range(3)] + [ask(lyst, analyst, "What was operating margin last quarter?")]
    _calls(lyst, tasks[0]["id"], "vendor/fast", 4, failed=2)
    _calls(lyst, tasks[3]["id"], "vendor/fast", 2)
    _calls(lyst, tasks[3]["id"], "vendor/deep", 1, cost=0.05)

    c = lyst.costs.costs(owner, WS, by="user")
    tot = c["totals"]
    assert tot["calls"] == 7 and tot["failed_calls"] == 2 and tot["retried_calls"] == 2
    assert tot["cost_usd"] == pytest.approx(0.062) and tot["retry_cost_usd"] == pytest.approx(0.004)
    assert tot["tasks"] == 4 and tot["compute_ms"] >= 0 and tot["queries"] > 0 and tot["database_bytes"] > 0
    by_user = {g["label"]: g for g in c["groups"]}
    assert by_user["Ana"]["cost_usd"] == pytest.approx(0.054) and by_user["Avery"]["retries"] == 2
    by_model = {g["key"]: g["cost_usd"] for g in lyst.costs.costs(owner, WS, by="model")["groups"]}
    assert by_model["vendor/deep"] == pytest.approx(0.05)
    by_task = {g["key"] for g in lyst.costs.costs(owner, WS, by="task")["groups"]}
    assert {tasks[0]["id"], tasks[3]["id"]} <= by_task

    v = lyst.costs.value(owner, WS)
    assert v["verified_answers"] == 4 and v["finished_tasks"] == 4 and v["verification_rate"] == 1.0
    assert "effort_avoided_hours" not in v  # never without the administrator's own assumption
    v = lyst.costs.value(owner, WS, manual_minutes_per_answer=45)
    assert v["effort_avoided_hours"]["estimate"] == 3.0 and "supplied by you" in v["effort_avoided_hours"]["assumption"]

    recs = {r["kind"]: r for r in lyst.costs.recommendations(owner, WS)}
    assert "asked 3 times" in recs["schedule_repeated_question"]["why"]
    assert "2 of 6 calls failed" in recs["reroute_model"]["why"]

    with pytest.raises(AccessDenied):
        lyst.costs.costs(analyst, WS)
    with pytest.raises(ValueError):
        lyst.costs.costs(owner, WS, by="moon")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as cl:
        assert cl.get(f"/v1/workspaces/{WS}/costs", params={"by": "model"}, headers=h).json()["totals"]["calls"] == 7
        assert cl.get(f"/v1/workspaces/{WS}/value", headers=h).json()["verified_answers"] == 4
        assert cl.get(f"/v1/workspaces/{WS}/costs/recommendations", headers=h).json()
