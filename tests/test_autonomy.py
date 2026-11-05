# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Event rules and autonomous activities: typed conditions, dry runs, governed activation, the Detect→Monitor
lifecycle, approvals before consequential work, bounded retries, duplicate and loop protection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lyst.api import create_app
from lyst.autonomy import TEMPLATES, RuleSpec, evaluate
from lyst.security import AccessDenied


def _settle(lyst, rounds: int = 4):
    for _ in range(rounds):
        lyst.events.dispatch()
        lyst.runtime.drain()
    lyst.events.dispatch()


def _start(lyst):
    lyst.events.dispatch()  # registers the durable consumers' cursors at the current end of the stream


def _reingest(lyst, owner, name, extra_col: str | None = None, drop: str | None = None):
    db = lyst.stores.workspace("northwind")
    phys = db.scalar("SELECT v.physical_table FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
                     "AND v.version = d.current_version WHERE d.name = ?", [name])
    with db.read() as c:
        df = c.execute(f'SELECT * FROM data."{phys}"').df()
    if extra_col:
        df[extra_col] = 1
    if drop:
        df = df.drop(columns=[drop])
    return lyst.data.ingest(owner, "northwind", name, df.to_csv(index=False).encode(), filename=f"{name}.csv")


def _rule(lyst, owner, template: str, **over):
    r = lyst.autonomy.save(owner, "northwind", TEMPLATES[template] | over)
    return lyst.autonomy.activate(owner, "northwind", r["id"])


def _approve_rule(lyst, owner, rule):
    lyst.runtime.decide(owner, rule["approval_id"], True)
    return lyst.autonomy._rule("northwind", rule["id"])


# -- the rule representation -------------------------------------------------------------------------


def test_rules_are_typed_and_refuse_anything_but_named_fields():
    with pytest.raises(ValidationError):
        RuleSpec.model_validate({"name": "bad", "on": ["x.y"], "when": {"field": "__import__('os')", "op": "eq", "value": 1},
                                 "action": {"kind": "notify"}})
    with pytest.raises(ValidationError):  # no rules on the engine's own records
        RuleSpec.model_validate({"name": "bad", "on": ["event.*"], "action": {"kind": "notify"}})
    deep = {"field": "data.n", "op": "gt", "value": 1}
    for _ in range(5):
        deep = {"all": [deep]}
    with pytest.raises(ValidationError):
        RuleSpec.model_validate({"name": "too deep", "on": ["x.y"], "when": deep, "action": {"kind": "notify"}})
    s = RuleSpec.model_validate({"name": "kpi", "on": ["external.*"], "action": {"kind": "notify"}, "when": {"any": [
        {"field": "data.value", "op": "pct_change_gte", "value": 20, "other": "data.baseline"},
        {"not": {"field": "sensitivity", "op": "in", "value": ["public", "internal"]}}]}})
    ok, why = evaluate(s.when, {"data": {"value": 130, "baseline": 100}, "sensitivity": "internal"})
    assert ok and "+30.0%" in why
    assert not evaluate(s.when, {"data": {"value": 105, "baseline": 100}, "sensitivity": "internal"})[0]


def test_a_dry_run_shows_what_a_rule_would_do_without_doing_it(seeded):
    """Scenario E (logic half): three failing syncs within the window make one match; earlier ones do not; nothing runs."""
    lyst, owner = seeded
    evs = [{"id": f"x{i}", "seq": i, "type": "connection.synced", "subject": {"kind": "connection", "id": "crm"},
            "data": {"outcome": "error"}, "occurred_at": f"2026-11-05T10:0{i}:00+00:00", "depth": 0} for i in range(1, 5)]
    out = lyst.autonomy.simulate(owner, "northwind", TEMPLATES["connector_failing"], events=evs)
    assert [r["decision"] for r in out["results"]] == ["no_match", "no_match", "act", "suppressed"]
    assert out["needs_approval"] is False
    assert lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM activities") == 0
    viewer = lyst.guard.create_user("vi@northwind.test", "Vi")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with pytest.raises(AccessDenied):
        lyst.autonomy.simulate(viewer, "northwind", TEMPLATES["connector_failing"], events=evs)
    with pytest.raises(AccessDenied):
        lyst.autonomy.save(viewer, "northwind", TEMPLATES["schema_change"])


# -- scenarios ----------------------------------------------------------------------------------------


def test_a_schema_change_opens_an_impact_review_with_its_dependents(seeded):
    """Scenario A: a schema change → a versioned event → the rule → an activity listing what depends on the dataset."""
    lyst, owner = seeded
    rule = _rule(lyst, owner, "schema_change")
    assert rule["status"] == "active" and rule["risk"] == "low"
    _start(lyst)
    _reingest(lyst, owner, "pnl", extra_col="channel")
    _settle(lyst)
    acts = lyst.autonomy.activities(owner, "northwind", subject=("dataset", "pnl"))
    assert len(acts) == 1
    a = lyst.autonomy.get(owner, "northwind", acts[0]["id"])
    assert a["status"] == "completed" and a["event"]["type"] == "dataset.schema_changed"
    assert [s["stage"] for s in a["stages"]] == ["detect", "interpret", "qualify", "plan", "authorize", "execute", "verify", "deliver"]
    assert any(x["kind"] == "metric" for x in a["outcome"]["affected"])  # demo metrics are computed from pnl
    assert "1 column added" in a["explanation"] and a["flow"][0] == {"node": "trigger", "label": "dataset.schema_changed",
                                                                     "ref": a["event"]["seq"]}
    # the activity is part of the event's causal chain
    chain = lyst.events.get(owner, "northwind", a["event"]["seq"])["chain"]
    assert {"dataset.schema_changed", "activity.opened", "activity.changed"} <= {e["type"] for e in chain}


def test_a_kpi_deviation_starts_an_investigation_that_delivers_an_artifact(seeded):
    """Scenarios B and C: a material deviation is validated by the rule's condition, an authorized read-only
    investigation runs, and when it finishes (whatever page the person is on) the activity holds the artifact."""
    lyst, owner = seeded
    spec = {"name": "Investigate a revenue deviation", "on": ["external.finance.kpi"], "autonomy": "investigate",
            "when": {"all": [{"field": "subject.id", "op": "eq", "value": "revenue"},
                             {"field": "data.value", "op": "pct_change_gte", "value": 15, "other": "data.baseline"}]},
            "action": {"kind": "investigate", "question": "What was {subject.id} last quarter?"}}
    r = lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    assert r["status"] == "active"
    _start(lyst)
    lyst.events.external(owner, "northwind", "external.finance.kpi", subject=("metric", "revenue"),
                         data={"value": 104, "baseline": 100}, dedupe_key=None)  # within tolerance: no activity
    lyst.events.external(owner, "northwind", "external.finance.kpi", subject=("metric", "revenue"),
                         data={"value": 130, "baseline": 100}, dedupe_key=None)
    _settle(lyst)
    acts = lyst.autonomy.activities(owner, "northwind")
    assert len(acts) == 1
    a = lyst.autonomy.get(owner, "northwind", acts[0]["id"])
    assert a["status"] == "completed", a["stages"]
    task = lyst.runtime.get(owner, a["task_id"])
    assert task["status"] == "done" and task["result"]["status"] == "complete"
    assert a["outcome"]["artifact_id"] and a["outcome"]["asset_id"]  # the verified briefing, kept as an artifact
    assert next(s for s in a["stages"] if s["stage"] == "verify")["status"] == "done"
    # the task's own events continue the KPI event's chain
    done = [e for e in lyst.events.since("northwind", 0, types=["task.completed"]) if e["subject"]["id"] == a["task_id"]]
    assert done[0]["correlation_id"] == a["correlation_id"] and done[0]["depth"] == 1


def test_a_consequential_proposal_waits_for_approval(seeded):
    """Scenarios D and F: a stale report → a repair proposal; nothing runs until a person approves, then the repair
    runs and the previous version is kept."""
    lyst, owner = seeded
    rule = _rule(lyst, owner, "broken_asset")
    assert rule["status"] == "pending_approval"  # a rule that changes things is not active until approved
    rule = _approve_rule(lyst, owner, rule)
    assert rule["status"] == "active"
    t = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.runtime.drain()
    task = lyst.runtime.get(owner, t)
    art = lyst.artifacts.build(owner, task, "md")
    asset_id = art["asset_id"]
    before = lyst.assets.get(owner, "northwind", asset_id)["version"]
    _start(lyst)
    _reingest(lyst, owner, "pnl", extra_col="channel")  # a newer version of its data: the report is stale
    _settle(lyst, 2)
    acts = lyst.autonomy.activities(owner, "northwind", subject=("asset", asset_id))
    assert len(acts) == 1 and acts[0]["status"] == "awaiting_approval"
    assert lyst.assets.get(owner, "northwind", asset_id)["health"] == "stale"  # nothing ran
    lyst.runtime.decide(owner, acts[0]["approval_id"], True)
    a = lyst.autonomy.get(owner, "northwind", acts[0]["id"])
    assert next(s for s in a["stages"] if s["stage"] == "authorize")["status"] == "approved"
    assert a["outcome"]["action"] == "rebuild" and a["status"] in ("running", "completed")
    _settle(lyst)
    after = lyst.assets.get(owner, "northwind", asset_id)
    assert after["version"] == before + 1 and after["health"] == "current"
    assert any(v["version"] == before for v in after["versions"])


def test_a_rejected_proposal_never_runs(seeded):
    lyst, owner = seeded
    rule = _approve_rule(lyst, owner, _rule(lyst, owner, "broken_asset"))
    t = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    lyst.runtime.drain()
    task = lyst.runtime.get(owner, t)
    asset_id = lyst.artifacts.build(owner, task, "md")["asset_id"]
    _start(lyst)
    _reingest(lyst, owner, "pnl", extra_col="channel")
    _settle(lyst, 2)
    act = lyst.autonomy.activities(owner, "northwind", subject=("asset", asset_id))[0]
    lyst.runtime.decide(owner, act["approval_id"], False)
    _settle(lyst)
    assert lyst.autonomy.get(owner, "northwind", act["id"])["status"] == "cancelled"
    assert lyst.assets.get(owner, "northwind", asset_id)["health"] == "stale"
    assert rule["status"] == "active"


def test_a_recoverable_failure_is_retried_once_and_an_unrecoverable_one_is_not(seeded):
    """Workflow reliability: a transient failure gets one bounded retry; an input error is left for a person."""
    lyst, owner = seeded
    _approve_rule(lyst, owner, _rule(lyst, owner, "recoverable_failure"))
    _start(lyst)
    ok = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?")
    bad = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?", idempotency_key="other")
    ctl = lyst.stores.control
    ctl.execute("UPDATE tasks SET status = 'failed', error = 'transient: the model provider timed out', updated_at = now() WHERE id = ?", [ok])
    ctl.execute("UPDATE tasks SET status = 'failed', error = 'input: unknown metric gross_vibes', updated_at = now() WHERE id = ?", [bad])
    lyst.events.dispatch()
    lyst.events.dispatch()
    by = {a["subject_id"]: a for a in lyst.autonomy.activities(owner, "northwind", limit=50)}
    assert lyst.runtime.get(owner, ok)["status"] in ("queued", "running", "done")
    assert by[ok]["status"] in ("running", "completed")
    assert by[bad]["status"] == "failed" and "not recoverable" in next(s["detail"] for s in by[bad]["stages"] if s["stage"] == "execute")
    assert lyst.runtime.get(owner, bad)["status"] == "failed"
    # the same task failing again within a day is suppressed, not retried again
    lyst.runtime.drain()
    ctl.execute("UPDATE tasks SET status = 'failed', error = 'transient: again', updated_at = now() + INTERVAL 1 SECOND WHERE id = ?", [ok])
    lyst.events.dispatch()
    lyst.events.dispatch()
    assert lyst.stores.control.scalar("SELECT count(*) FROM task_events WHERE task_id = ? AND kind = 'retry'", [ok]) == 1


def test_a_redelivered_event_opens_one_activity(seeded):
    """Scenario G at the rule layer: the same event delivered again (a replayed cursor) is not acted on twice."""
    lyst, owner = seeded
    _rule(lyst, owner, "schema_change")
    _start(lyst)
    _reingest(lyst, owner, "pnl", extra_col="channel")
    _settle(lyst)
    lyst.stores.control.execute("UPDATE event_consumers SET position = 0 WHERE name = 'autonomy.rules'")
    lyst.stores.control.execute("DELETE FROM event_inbox WHERE consumer = 'autonomy.rules'")  # even with the inbox lost
    _settle(lyst)
    assert len(lyst.autonomy.activities(owner, "northwind", subject=("dataset", "pnl"))) == 1


def test_rules_that_feed_each_other_stop_at_the_chain_limit(seeded, monkeypatch):
    """Scenario L: a rule that reacts to activities would open activities for ever; the chain limit stops it."""
    monkeypatch.setenv("LYST_EVENT_MAX_DEPTH", "4")
    lyst, owner = seeded
    spec = {"name": "Echo every activity", "on": ["activity.opened", "external.loop.*"], "autonomy": "prepare",
            "suppress_seconds": 0, "max_per_day": 500, "action": {"kind": "notify", "summary": "echo"}}
    lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    _start(lyst)
    lyst.events.external(owner, "northwind", "external.loop.start", subject=("loop", "1"), data={}, dedupe_key=None)
    for _ in range(15):
        lyst.events.dispatch()
    n = lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM activities")
    assert 1 < n <= 4
    assert lyst.stores.workspace("northwind").scalar("SELECT max(depth) FROM activities") < 4


def test_a_suggestion_waits_for_a_person_and_the_api_runs_it(seeded):
    lyst, owner = seeded
    spec = TEMPLATES["schema_change"] | {"autonomy": "suggest"}
    lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    _start(lyst)
    _reingest(lyst, owner, "pnl", extra_col="channel")
    _settle(lyst)
    act = lyst.autonomy.activities(owner, "northwind")[0]
    assert act["status"] == "suggested" and act["outcome"] is None
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.get("/v1/rule-templates", headers=h).json()["templates"]["schema_change"]["action"]["kind"] == "impact_review"
        assert len(c.get("/v1/workspaces/northwind/rules", headers=h).json()) == 1
        r = c.post(f"/v1/workspaces/northwind/activities/{act['id']}/run", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "completed"
        assert c.post(f"/v1/workspaces/northwind/activities/{act['id']}/run", headers=h).status_code == 400
        assert c.get(f"/v1/workspaces/northwind/activities/{act['id']}", headers=h).json()["flow"][1]["node"] == "rule"
        other = lyst.guard.create_user("eve@else.test", "Eve")
        lyst.guard.create_workspace(other, "elsewhere", "Elsewhere")
        he = {"Authorization": f"Bearer {lyst.guard.issue_token(other, 'test')}"}
        assert c.get("/v1/workspaces/northwind/activities", headers=he).status_code == 403
        bad = c.post("/v1/workspaces/northwind/rules", headers=h, json={"spec": {"name": "x", "on": ["a.b"], "action": {"kind": "nope"}}})
        assert bad.status_code in (400, 422)


def test_a_monitor_finding_becomes_an_event_a_rule_investigates(seeded):
    """Scenario B end to end: a monitor validates a deviation (robust shift), the finding is staged as metric.deviated,
    the rule investigates it, and the findings are kept as a report."""
    lyst, owner = seeded
    _rule(lyst, owner, "metric_deviation")
    _start(lyst)
    m = lyst.monitors.create(owner, "northwind", metric="operating_margin", grain="month", investigate=False)
    lyst.runtime.decide(owner, m["approval_id"], True)
    found = lyst.monitors.tick("northwind")
    assert any(f["kind"] == "metric_shift" for f in found)
    _settle(lyst)
    acts = lyst.autonomy.activities(owner, "northwind", subject=("metric", "operating_margin"))
    assert len(acts) == 1, acts
    a = acts[0]
    assert a["status"] == "completed" and a["outcome"]["asset_id"], a["stages"]
    assert lyst.runtime.get(owner, a["task_id"])["result"]["status"] == "complete"


def test_a_prediction_alone_never_starts_consequential_work(seeded):
    lyst, owner = seeded
    spec = {"name": "Retry on a forecast", "on": ["anticipation.found"], "autonomy": "automate",
            "action": {"kind": "retry_task"}}
    r = lyst.autonomy.activate(owner, "northwind", lyst.autonomy.save(owner, "northwind", spec)["id"])
    lyst.runtime.decide(owner, r["approval_id"], True)
    _start(lyst)
    lyst.events.emit("northwind", "anticipation.found", subject=("task", "t_x"), source="service", data={"score": 0.9})
    _settle(lyst, 1)
    assert lyst.autonomy.activities(owner, "northwind")[0]["status"] == "suggested"
