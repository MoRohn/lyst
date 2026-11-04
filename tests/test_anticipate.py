# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Anticipation core: prepared-work objects, autonomy levels, budgets, invalidation and the person's control.
Acceptance scenarios A (upcoming financial review), E (anticipative workflow preparation) and J (user control)."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.app import Lyst
from lyst.config import load_settings
from lyst.connectors.rest import RestConnector

WS = "northwind"
NOW = lambda: datetime.now(UTC)  # noqa: E731


def _schedule(lyst, owner, title, question, *, due_in=timedelta(minutes=30), every=2592000):
    s = lyst.runtime.create_schedule(owner, WS, title, question, every)
    lyst.runtime.decide(owner, s["approval_id"], True)
    lyst.stores.control.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", [NOW() + due_in, s["schedule_id"]])
    return s["schedule_id"]


def _level(lyst, owner, level, **kw):
    return lyst.anticipation.set_settings(owner, WS, {"level": level, **kw})


def _counts(lyst):
    db = lyst.stores.workspace(WS)
    return {"tasks": lyst.stores.control.scalar("SELECT count(*) FROM tasks"),
            "approvals": lyst.stores.control.scalar("SELECT count(*) FROM approvals WHERE status = 'pending'"),
            "versions": db.scalar("SELECT count(*) FROM dataset_versions"),
            "metric_versions": db.scalar("SELECT count(*) FROM metric_versions"),
            "artifacts": db.scalar("SELECT count(*) FROM assets")}


def test_a_upcoming_financial_review_is_prepared_without_distributing_or_editing(seeded):
    lyst, owner = seeded
    sid = _schedule(lyst, owner, "Monthly finance review", "Review revenue and operating margin for the latest month")
    _level(lyst, owner, 2)
    before = _counts(lyst)
    out = lyst.anticipation.cycle(WS)
    assert out["anticipated"] == 1 and out["prepared"] == 1, out
    [a] = lyst.anticipation.list(owner, WS)
    assert a["kind"] == "recurring_prep" and a["subject_id"] == sid and a["status"] == "prepared"
    full = lyst.anticipation.get(owner, WS, a["id"])
    assert set(full["prepared"]["metrics"]) >= {"revenue", "operating_margin"} and "pnl" in full["prepared"]["datasets"]
    checks = {c["check"]: c for c in full["verification"]["checks"]}
    assert checks["access"]["status"] == "ok" and checks["freshness:pnl"]["status"] == "ok"
    assert checks["definition:revenue"]["status"] == "ok"
    assert a["verdict"] == "ready" and a["finding_id"] is None          # ready work is not pushed to the inbox
    # explicit authorization only: nothing was distributed, edited, queued or put up for approval while preparing
    assert _counts(lyst) == before
    assert [x["id"] for x in a["actions"]] == ["run_now", "dismiss", "snooze"]
    why = lyst.anticipation.explain(owner, WS, a["id"])
    assert why["short"] == why["why"][0] and why["detail"][0] == why["short"] and len(why["detail"]) > len(why["why"])
    assert "Monthly finance review" in why["why"][0] and "next run" in why["why"][0] and why["methods"].startswith("metadata")
    assert lyst.anticipation.cycle(WS)["anticipated"] == 0              # the same occurrence is one object
    r = lyst.anticipation.act(owner, WS, a["id"], "run_now")
    task = lyst.runtime.get(owner, r["task_id"])
    assert task["created_by"] == owner.user_id and task["status"] == "queued"
    with pytest.raises(ValueError):
        lyst.anticipation.act(owner, WS, a["id"], "run_now")            # already accepted


def test_a_a_blocker_is_found_before_the_run_and_surfaced_once(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue for the latest month")
    _level(lyst, owner, 2)
    lyst.stores.workspace(WS).execute("UPDATE metrics SET status = 'deprecated' WHERE name = 'revenue'")
    out = lyst.anticipation.cycle(WS)
    assert out["surfaced"] == 1, out
    [a] = lyst.anticipation.list(owner, WS)
    assert a["verdict"] == "blocked" and a["status"] == "surfaced" and a["finding_id"]
    inbox = lyst.monitors.inbox(owner, WS)
    hit = [i for i in inbox if i["id"] == a["finding_id"]]
    assert hit and "no longer approved" in hit[0]["summary"]
    assert lyst.anticipation.cycle(WS)["surfaced"] == 0                 # no repeat for the same condition


def test_prepared_work_is_invalidated_when_its_data_changes(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue for the latest month")
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    frame = demo.pnl()
    lyst.data.ingest(owner, WS, "pnl", demo.csv_bytes(frame.iloc[:-3]), filename="pnl.csv")   # after_ingest re-checks
    full = lyst.anticipation.get(owner, WS, a["id"])
    assert full["revision"] == 2 and full["status"] == "prepared"
    inval = [e for e in full["events"] if e["event"] == "invalidated"]
    assert inval and any("datasets.pnl" in c for c in inval[0]["detail"]["changed"])


def test_levels_gate_what_runs(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue for the latest month")
    _level(lyst, owner, 0)
    assert lyst.anticipation.cycle(WS)["observed"] == 0
    _level(lyst, owner, 1)                                            # suggest: listed, nothing executed
    out = lyst.anticipation.cycle(WS)
    assert out["anticipated"] == 1 and out["prepared"] == 0
    [a] = lyst.anticipation.list(owner, WS)
    assert a["status"] == "candidate" and a["verdict"] is None
    assert lyst.anticipation.settings(owner, WS)["usage_today"] == {}


def test_far_future_runs_are_not_prepared_yet(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue", due_in=timedelta(days=3))
    _level(lyst, owner, 2)
    assert lyst.anticipation.cycle(WS)["observed"] == 0               # just in time, not days ahead


def test_budget_refuses_preparation_and_counts_the_refusal(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    _level(lyst, owner, 2, daily_prepare_limit=0)
    out = lyst.anticipation.cycle(WS)
    assert out["prepared"] == 0 and out["refused"] and "budget" in out["refused"][0]
    assert lyst.anticipation.settings(owner, WS)["usage_today"]["prepare"]["refused"] == 1


def test_only_admins_set_the_level_and_people_see_their_own_items(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("vic@northwind.test", "Vic")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    from lyst.security import AccessDenied

    with pytest.raises(AccessDenied):
        lyst.anticipation.set_settings(viewer, WS, {"level": 4})
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    assert lyst.anticipation.list(viewer, WS) == []                   # the owner's schedule is not the viewer's business
    [a] = lyst.anticipation.list(owner, WS)
    with pytest.raises(KeyError):
        lyst.anticipation.act(viewer, WS, a["id"], "run_now")
    with pytest.raises(ValueError):
        lyst.anticipation.set_settings(owner, WS, {"level": 7})


def test_snooze_hides_until_then_and_dismiss_is_final(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    lyst.anticipation.act(owner, WS, a["id"], "snooze", until=NOW() + timedelta(hours=2))
    assert lyst.anticipation.list(owner, WS) == []
    assert lyst.anticipation.list(owner, WS, now=NOW() + timedelta(hours=3))[0]["id"] == a["id"]
    lyst.anticipation.act(owner, WS, a["id"], "dismiss")
    lyst.anticipation.cycle(WS)
    assert lyst.anticipation.list(owner, WS, now=NOW() + timedelta(hours=3)) == []   # not re-raised for this occurrence


def test_j_user_control_withdraws_cancels_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("LYST_MASTER_KEY", "ab" * 32)
    home = tmp_path / "home"
    lyst = Lyst.build(load_settings(home), lease_seconds=30)
    owner = lyst.guard.create_user("avery@northwind.test", "Avery")
    demo.seed(lyst, owner)
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    # a background task the engine queued for this item (investigations do this) is cancelled when the level drops
    tid = lyst.runtime.submit(owner, WS, "Investigate revenue", kind="investigation")
    lyst.stores.workspace(WS).execute("UPDATE anticipations SET task_id = ? WHERE id = ?", [tid, a["id"]])
    out = _level(lyst, owner, 0)
    assert out["withdrawn"] == 1 and out["level_name"] == "off"
    assert lyst.runtime.get(owner, tid)["status"] == "cancelled"
    assert lyst.anticipation.list(owner, WS) == []
    lyst.close()
    again = Lyst.build(load_settings(home), lease_seconds=30)
    try:
        assert again.anticipation.settings(owner, WS)["level"] == 0           # the preference survived a restart
        assert again.anticipation.cycle(WS) == {"level": 0, "observed": 0, "anticipated": 0, "invalidated": 0, "prepared": 0,
                                                "surfaced": 0, "expired": 0, "refused": []}
    finally:
        again.close()


def test_expired_preparation_closes_itself(seeded):
    lyst, owner = seeded
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    out = lyst.anticipation.cycle(WS, NOW() + timedelta(hours=3))
    assert out["expired"] == 1


# -- scenario E: a recurring data-heavy run fed by a connection ------------------------------------------------------------

class Orders:
    def __init__(self):
        self.records = [{"id": i, "updated_at": f"2026-01-{i:02d}", "amount": i * 10} for i in range(1, 6)]

    def __call__(self, req: httpx.Request):
        since = req.url.params.get("updated_after")
        return httpx.Response(200, json={"data": [r for r in self.records if not since or r["updated_at"] > since]})


@pytest.fixture
def orders(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_REST_ALLOWED_HOSTS", "api.example.test")
    src = Orders()
    RestConnector.http = httpx.Client(transport=httpx.MockTransport(src))
    conn = lyst.connections.create(owner, WS, kind="rest", name="Orders API", values={
        "url": "https://api.example.test/v1/orders", "records_key": "data", "since_param": "updated_after"})
    lyst.connections.sync(owner, WS, conn["id"], source_table="orders", cursor_column="updated_at")
    lyst.stores.workspace(WS).execute("UPDATE dataset_versions SET created_at = created_at - INTERVAL 1 HOUR WHERE dataset = 'orders'")
    sid = _schedule(lyst, owner, "Weekly order totals", "Total amount of orders by month", every=604800)
    # the previous run, after the last sync, read the orders dataset
    tid = "t_" + uuid.uuid4().hex[:12]
    lyst.stores.control.execute(
        "INSERT INTO tasks (id, workspace_id, created_by, kind, title, request, status, autonomy, created_at, updated_at, schedule_id) "
        "VALUES (?, ?, ?, 'scheduled', 'Weekly order totals', '{}', 'done', 'autonomous', ?, ?, ?)",
        [tid, WS, owner.user_id, NOW() - timedelta(seconds=40), NOW() - timedelta(seconds=20), sid])
    lyst.stores.workspace(WS).execute("INSERT INTO query_history (id, actor, task_id, sql, rows, ms, outcome) "
                                      "VALUES (nextval('query_seq'), ?, ?, 'SELECT sum(amount) FROM \"orders\"', 1, 3, 'ok')",
                                      [owner.user_id, tid])
    src.records += [{"id": 6, "updated_at": "2026-01-06", "amount": 60}]
    yield lyst, owner, conn
    RestConnector.http = None


def test_e_prepare_warns_and_automate_refreshes_the_input_before_the_run(orders):
    lyst, owner, conn = orders
    db = lyst.stores.workspace(WS)
    _level(lyst, owner, 2)
    lyst.anticipation.cycle(WS)
    [a] = lyst.anticipation.list(owner, WS)
    full = lyst.anticipation.get(owner, WS, a["id"])
    assert "orders" in full["prepared"]["datasets"]                   # learned from what the last run queried
    fresh = {c["check"]: c for c in full["verification"]["checks"]}["freshness:orders"]
    assert fresh["status"] == "warn" and "no new data" in fresh["detail"] and a["verdict"] == "ready_with_warnings"
    v_before = db.scalar("SELECT current_version FROM datasets WHERE name = 'orders'")
    assert full["prepared"]["refreshed"] == []                        # level 2 never writes
    _level(lyst, owner, 4)
    db.execute("UPDATE anticipations SET status = 'candidate' WHERE id = ?", [a["id"]])   # re-prepare at the new level
    lyst.anticipation.cycle(WS)
    full = lyst.anticipation.get(owner, WS, a["id"])
    assert full["prepared"]["refreshed"] and full["prepared"]["refreshed"][0]["rows_added"] == 1
    assert db.scalar("SELECT current_version FROM datasets WHERE name = 'orders'") == v_before + 1
    assert full["resources"]["refreshes"] == 1 and full["resources"]["model_calls"] == 0
    assert full["resources"]["compute_ms"] < 5000                     # bounded: one incremental sync, no analysis


def test_api_routes(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner)
    _schedule(lyst, owner, "Monthly finance review", "Review revenue")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        h = {"Authorization": f"Bearer {token}"}
        assert c.get(f"/v1/workspaces/{WS}/anticipation/settings", headers=h).json()["level_name"] == "suggest"
        r = c.patch(f"/v1/workspaces/{WS}/anticipation/settings", headers=h, json={"level": "prepare"})
        assert r.status_code == 200 and r.json()["level"] == 2
        assert c.patch(f"/v1/workspaces/{WS}/anticipation/settings", headers=h, json={"level": 9}).status_code == 400
        assert c.post(f"/v1/workspaces/{WS}/anticipations/refresh", headers=h).json()["prepared"] == 1
        [a] = c.get(f"/v1/workspaces/{WS}/anticipations", headers=h).json()
        assert c.get(f"/v1/workspaces/{WS}/anticipations/{a['id']}/why", headers=h).json()["why"]
        assert c.get(f"/v1/workspaces/{WS}/anticipations/{a['id']}", headers=h).json()["events"][0]["event"] == "anticipated"
        r = c.post(f"/v1/workspaces/{WS}/anticipations/{a['id']}/act", headers=h, json={"action": "snooze"})
        assert r.json()["status"] == "snoozed"
        assert c.post(f"/v1/workspaces/{WS}/anticipations/{a['id']}/act", headers=h, json={"action": "bogus"}).status_code == 400
        assert c.get(f"/v1/workspaces/{WS}/anticipations/ant_missing", headers=h).status_code == 404
