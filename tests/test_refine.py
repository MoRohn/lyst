# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Refinement pass 6: one task per submission, and a trace of where a task's time, model calls and queries went."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app

from .conftest import ask


def _h(lyst, user):
    return {"Authorization": f"Bearer {lyst.guard.issue_token(user)}"}


def test_a_repeated_submission_with_the_same_key_runs_once(seeded):
    """Before: the same POST sent twice (a double click, a client retry after a timeout) queued two tasks."""
    lyst, owner = seeded
    h = _h(lyst, owner) | {"Idempotency-Key": "k-1"}
    body = {"question": "What was revenue last quarter?"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        with ThreadPoolExecutor(8) as pool:
            ids = set(pool.map(lambda _: c.post("/v1/workspaces/northwind/tasks", json=body, headers=h).json()["task_id"],
                               range(8)))
        assert len(ids) == 1
        # a new key is a new request; no key keeps the old behaviour
        other = c.post("/v1/workspaces/northwind/tasks", json=body, headers=_h(lyst, owner) | {"Idempotency-Key": "k-2"})
        plain = [c.post("/v1/workspaces/northwind/tasks", json=body, headers=_h(lyst, owner)).json()["task_id"]
                 for _ in range(2)]
    assert other.json()["task_id"] not in ids and plain[0] != plain[1]
    n = lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE workspace_id = 'northwind'")
    assert n == 4


def test_an_idempotency_key_never_returns_someone_elses_task(seeded):
    lyst, owner = seeded
    editor = lyst.guard.create_user("e@n.test", "E")
    lyst.guard.add_member(owner, "northwind", editor.user_id, "editor")
    a = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?", idempotency_key="same")
    b = lyst.runtime.submit(editor, "northwind", "What was revenue last quarter?", idempotency_key="same")
    assert a != b
    assert lyst.runtime.submit(owner, "northwind", "anything", idempotency_key="same") == a


def test_trace_accounts_for_steps_queries_and_model_calls(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    assert t["status"] == "done"
    # a model call recorded while exactly one step ran (as the gateway records it) is attributed to that step
    step = next(s for s in lyst.runtime.trace(owner, t["id"])["steps"] if s["capability"] == "deliver")
    lyst.stores.control.execute(
        "INSERT INTO llm_calls (id, occurred_at, workspace_id, task_id, model, purpose, outcome, latency_ms, "
        "input_tokens, output_tokens, cost_usd) VALUES (nextval('llm_call_seq'), ?, 'northwind', ?, 'm', 'narrate', 'ok', "
        "1200, 800, 300, 0.0004)", [step["started_at"], t["id"]])
    tr = lyst.runtime.trace(owner, t["id"])
    s = tr["summary"]
    assert s["steps"] == len(t["steps"]) and s["steps"] > 3
    assert s["queries"] > 0 and s["queries"] == len(tr["queries"])
    assert s["query_ms"] == sum(q["ms"] or 0 for q in tr["queries"])
    assert s["model_calls"] == 1 and s["model_ms"] == 1200 and s["cost_usd"] == 0.0004
    assert tr["model_calls"][0]["step"] == step["seq"]
    assert sum(x["queries"] for x in tr["steps"]) <= s["queries"]  # attributed only when unambiguous
    assert s["wall_ms"] is not None and s["wall_ms"] >= 0 and s["slowest_step"]
    assert not any("dataset" in k or "rows_data" in k for k in tr)  # timing and SQL text only, no result rows


def test_trace_obeys_workspace_access(seeded):
    lyst, owner = seeded
    t = ask(lyst, owner, "What was revenue last quarter?")
    stranger = lyst.guard.create_user("x@elsewhere.test", "X")
    lyst.guard.create_workspace(stranger, "elsewhere", "Elsewhere")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get(f"/v1/tasks/{t['id']}/trace", headers=_h(lyst, owner)).status_code == 200
        r = c.get(f"/v1/tasks/{t['id']}/trace", headers=_h(lyst, stranger))
        assert r.status_code in (403, 404) and "SELECT" not in r.text
        assert c.get("/v1/tasks/t_missing/trace", headers=_h(lyst, owner)).status_code == 404


# --------------------------------------------------------------------------------------- interface

from streamlit.testing.v1 import AppTest  # noqa: E402

from .test_ui_pages import _texts  # noqa: E402
from .test_ui_pages import ui as ui  # noqa: E402,F401  (the fixture)


def _settings(view: str | None = None):
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.run()
    at.switch_page("app_pages/settings.py")
    if view:
        at.query_params["view"] = view
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.mark.parametrize("view,expect", [("models", "OpenRouter"), ("connections", "Credentials of"),
                                         ("workspace", "Audit trail")])
def test_settings_sections_render_without_raw_internals(ui, view, expect):
    at = _settings(view)
    body = _texts(at)
    assert expect in body
    assert "{'interactive'" not in body and "Gateway now" not in body  # no Python dicts on screen
    assert not at.get("metric")  # the four large tiles are gone; the header carries role, encryption and calls


def test_workspace_section_lists_members_as_rows_with_readable_times(ui):
    at = _settings("workspace")
    body = _texts(at)
    assert "avery@northwind.test" in body or "Avery" in body
    df = at.dataframe[-1].value
    assert list(df.columns) == ["When", "Who", "Action", "On", "Outcome", "Detail"]
    assert "avery@northwind.test" in set(df["Who"])  # actors by email, not user ids
    assert not any(str(v).startswith("20") and "T" in str(v) for v in df["When"])  # no ISO stamps


def test_answer_offers_how_this_ran(ui):
    lyst = ui
    tid = lyst.stores.control.scalar("SELECT id FROM tasks WHERE status = 'done' ORDER BY created_at LIMIT 1")
    at = AppTest.from_file("../src/lyst/ui/app.py", default_timeout=30)
    at.query_params["task"] = tid
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    btn = next(b for b in at.button if b.label == "How this ran")
    btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert "steps" in _texts(at) and "governed quer" in _texts(at)


def test_engine_restart_resumes_interrupted_tasks_at_once_not_after_the_lease(seeded):
    """Measured live before the fix: SIGKILL of the engine mid-burst, restart up in seconds, but the two tasks that
    were running sat idle for their whole 30 s lease. Their lease can only belong to the dead process."""
    lyst, owner = seeded
    tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.stores.control.execute(
        "UPDATE tasks SET status = 'running', lease_owner = 'w_deadproc', lease_until = now() + INTERVAL 30 SECOND "
        "WHERE id = ?", [tid])
    assert lyst.runtime.drain() == 0  # without recovery nothing can take it for another 30 s
    assert lyst.runtime.recover_orphans() == 1
    lyst.runtime.drain()
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "done" and t["outcome"] == "complete"
    assert any(e["kind"] == "recovered" for e in t["events"])
    assert lyst.stores.control.scalar("SELECT takeovers FROM tasks WHERE id = ?", [tid]) == 1  # still counted
    assert lyst.runtime.recover_orphans() == 0  # nothing left; its own leases are never touched


def test_a_hung_step_cannot_hold_its_task_or_worker_past_the_time_budget(seeded, monkeypatch):
    """Before: the budget was checked only between waves, so a task with a 1 s budget held its worker for the whole
    6 s of a slow step (measured 6.16 s), and a step that never returned held it forever while the heartbeat kept
    renewing the lease."""
    import threading
    import time

    lyst, owner = seeded
    reg = lyst.runtime.services.registry
    orig, release = reg.run, threading.Event()

    def hung(name, ctx, args):
        if name == "deliver":
            release.wait(20)
        return orig(name, ctx, args)

    monkeypatch.setattr(reg, "run", hung)
    tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?", budget_seconds=1)
    t0 = time.monotonic()
    lyst.runtime.drain()
    assert time.monotonic() - t0 < 3
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed" and "deliver" in t["error"] and "time budget" in t["error"]
    step = next(s for s in t["steps"] if s["capability"] == "deliver")
    assert step["status"] == "failed" and step["error"].startswith("timeout")
    release.set()  # the stray step finishes later; its checkpoint must not land on the failed task
    time.sleep(1.0)
    t = lyst.runtime.get(owner, tid)
    assert t["status"] == "failed"
    assert next(s for s in t["steps"] if s["capability"] == "deliver")["status"] == "failed"


def test_every_query_records_the_step_that_ran_it_even_in_a_parallel_wave(seeded):
    """Pass 6 left queries of parallel steps unattributed (three metric_value steps run in one wave)."""
    lyst, owner = seeded
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    tr = lyst.runtime.trace(owner, t["id"])
    assert tr["queries"] and all(q["step"] is not None for q in tr["queries"])
    assert sum(s["queries"] for s in tr["steps"]) == tr["summary"]["queries"]
    by_cap = {s["capability"]: s["queries"] for s in tr["steps"]}
    assert by_cap.get("metric_value", 0) > 0 or any(s["queries"] for s in tr["steps"] if s["capability"] == "metric_value")
