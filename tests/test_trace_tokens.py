# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""How this ran: tokens per step, from the model calls attributed to it, and a dash where there are none."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from .conftest import ask
from .test_ui_pages import ui as ui  # noqa: F401  (the fixture)


def _call(lyst, at, task, outcome, tin, tout):
    lyst.stores.control.execute(
        "INSERT INTO llm_calls (id, occurred_at, workspace_id, task_id, model, purpose, outcome, latency_ms, "
        "input_tokens, output_tokens, cost_usd) VALUES (nextval('llm_call_seq'), ?, 'northwind', ?, 'm', 'deliver', ?, "
        "100, ?, ?, 0)", [at, task, outcome, tin, tout])


def test_each_step_sums_the_tokens_of_its_model_calls(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    steps = lyst.runtime.trace(owner, t["id"])["steps"]
    deliver = next(s for s in steps if s["capability"] == "deliver")
    plan = next(s for s in steps if s["capability"] == "plan")
    _call(lyst, deliver["started_at"], t["id"], "rejected", None, None)  # a refused call reports no tokens
    _call(lyst, deliver["started_at"], t["id"], "ok", 95, 60)
    _call(lyst, plan["started_at"], t["id"], "rejected", None, None)
    by_cap = {s["capability"]: s for s in lyst.runtime.trace(owner, t["id"])["steps"]}
    d = by_cap["deliver"]
    assert (d["model_calls"], d["input_tokens"], d["output_tokens"], d["tokens"]) == (2, 95, 60, 155)
    # a step whose only call was refused, and a step with no model calls, have no tokens rather than zero
    assert by_cap["plan"]["model_calls"] == 1 and by_cap["plan"]["tokens"] is None
    assert by_cap["understand"]["model_calls"] == 0 and by_cap["understand"]["tokens"] is None


def test_how_this_ran_shows_a_tokens_column_with_dashes_for_none(ui):
    lyst = ui
    tid = lyst.stores.control.scalar("SELECT id FROM tasks WHERE status = 'done' ORDER BY created_at LIMIT 1")
    started = lyst.stores.control.scalar(
        "SELECT started_at FROM task_steps WHERE task_id = ? AND capability = 'deliver'", [tid])
    _call(lyst, started, tid, "rejected", None, None)
    _call(lyst, started, tid, "ok", 1200, 300)
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.query_params["task"] = tid
    at.run()
    next(b for b in at.button if b.label == "How this ran").click().run()
    assert not at.exception, [e.value for e in at.exception]
    steps, calls = at.dataframe[-2].value, at.dataframe[-1].value
    assert list(steps.columns).index("Tokens") == list(steps.columns).index("Model calls") + 1
    tokens = dict(zip(steps["Step"].str.split(". ", n=1).str[1], steps["Tokens"], strict=False))
    assert "1,500 (1,200 in, 300 out)" in tokens.values()
    assert tokens["Reading the request"] == "—"
    assert "None" not in steps["Tokens"].astype(str).tolist()
    assert calls["Tokens in"].isna().sum() == 1  # rendered with the dash placeholder, not "None"
