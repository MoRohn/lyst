# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The event engine: canonical envelope, transactional outbox, durable consumers (at least once, idempotent, dead
letters), causal chains with a depth limit, the SSE stream with replay, and workspace isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyst import events as ev_mod
from lyst.api import create_app
from lyst.events import Event, PermanentFailure
from lyst.security import AccessDenied


def _ingest(lyst, owner, csv: bytes, name="targets", ws="northwind"):
    return lyst.data.ingest(owner, ws, name, csv, filename=f"{name}.csv")


def _due(lyst):
    lyst.stores.control.execute("UPDATE event_inbox SET next_attempt_at = now() WHERE status = 'retry'")


def test_a_schema_change_is_one_versioned_event_staged_with_the_version(seeded):
    """Scenario A, phase 1: a dataset's schema changes; Lyst emits one versioned, correlated event for it."""
    lyst, owner = seeded
    _ingest(lyst, owner, b"region,target\nEMEA,10\nAPAC,8\n")
    start = lyst.events.list(owner, "northwind", after=-1)["next"]
    _ingest(lyst, owner, b"region,target,owner\nEMEA,10,ana\nAPAC,8,bo\n")  # adds a column
    _ingest(lyst, owner, b"region,target,owner\nEMEA,10,ana\nAPAC,8,bo\n")  # same bytes: no new version, no event
    got = [e for e in lyst.events.list(owner, "northwind", after=start)["events"] if e["type"] == "dataset.schema_changed"]
    assert len(got) == 1
    e = Event.model_validate(got[0])  # the envelope is the documented contract
    assert e.subject.kind == "dataset" and e.subject.id == "targets" and e.source == "service" and e.schema_ == 2
    assert e.data["added"] == ["owner"] and e.data["removed"] == [] and e.data["version"] == 2
    assert e.correlation_id == e.id and e.depth == 0 and e.dedupe_key == "schema:targets:v2"
    # nothing left unpublished in the outbox
    assert lyst.stores.workspace("northwind").scalar("SELECT count(*) FROM event_outbox WHERE published_at IS NULL") == 0


def test_the_same_fact_delivered_many_times_is_one_event(seeded):
    """Scenario G: a repeated emission (same dedupe key) and a re-published outbox row (a crash between the two
    writes) never make a second event."""
    lyst, owner = seeded
    for _ in range(3):
        lyst.events.emit("northwind", "dataset.schema_changed", subject=("dataset", "x"), dedupe_key="k1")
    db = lyst.stores.workspace("northwind")
    db.execute("UPDATE event_outbox SET published_at = NULL")  # as if the process died before stamping them
    lyst.events._pending.add("northwind")
    lyst.events.collect()
    n = lyst.stores.control.scalar("SELECT count(*) FROM platform_events WHERE workspace_id = 'northwind' AND dedupe_key = 'k1'")
    assert n == 1


def test_a_durable_consumer_retries_then_succeeds_once_and_skips_redelivery(seeded):
    lyst, owner = seeded
    calls: list[str] = []

    def handler(e):
        calls.append(e["id"])
        if len(calls) < 3:
            raise RuntimeError("not yet")

    lyst.events.consume("test.flaky", handler, types=["external.*"], max_attempts=5)
    lyst.events.dispatch()  # registers the cursor at the current end
    ev = lyst.events.external(owner, "northwind", "external.crm.deal_closed", subject=("deal", "d1"), data={"amount": 5},
                              dedupe_key=None)
    lyst.events.dispatch()
    _due(lyst)
    lyst.events.dispatch()
    _due(lyst)
    lyst.events.dispatch()
    assert calls == [ev["id"]] * 3
    state = [p for p in lyst.events.get(owner, "northwind", ev["seq"])["processing"] if p["consumer"] == "test.flaky"]
    assert state == [{"consumer": "test.flaky", "status": "done", "attempts": 3, "error": None,
                      "updated_at": state[0]["updated_at"]}]
    # redelivery (a replayed cursor) does not run the handler again
    lyst.stores.control.execute("UPDATE event_consumers SET position = ? WHERE name = 'test.flaky'", [ev["seq"] - 1])
    lyst.events.dispatch()
    assert len(calls) == 3


def test_a_poison_event_is_dead_lettered_and_the_stream_moves_on(seeded):
    lyst, owner = seeded
    seen: list[str] = []

    def handler(e):
        if e["data"].get("bad"):
            raise PermanentFailure("cannot be processed")
        seen.append(e["subject"]["id"])

    lyst.events.consume("test.poison", handler, types=["external.*"])
    lyst.events.dispatch()
    bad = lyst.events.external(owner, "northwind", "external.erp.row", subject=("row", "1"), data={"bad": True}, dedupe_key=None)
    lyst.events.external(owner, "northwind", "external.erp.row", subject=("row", "2"), data={}, dedupe_key=None)
    lyst.events.dispatch()
    assert seen == ["2"]
    dead = lyst.events.dead_letters(owner, "northwind")
    assert [d["event"]["id"] for d in dead] == [bad["id"]] and "PermanentFailure" in dead[0]["error"]
    lyst.events.collect()
    assert any(e["type"] == "event.dead_lettered" for e in lyst.events.since("northwind", bad["seq"]))
    # an admin puts it back after the fix
    handler_fixed = []
    lyst.events.consumers["test.poison"].handler = lambda e: handler_fixed.append(e["id"])
    lyst.events.retry_dead(owner, "northwind", "test.poison", bad["seq"])
    lyst.events.dispatch()
    assert handler_fixed == [bad["id"]]


def test_rules_that_trigger_each_other_stop_at_the_chain_limit(seeded, monkeypatch):
    """Scenario L: a consumer that answers every event with another one (a loop) is stopped at the depth limit,
    with one chain_stopped record, instead of running for ever."""
    monkeypatch.setenv("LYST_EVENT_MAX_DEPTH", "4")
    lyst, owner = seeded

    def echo(e):
        lyst.events.emit(e["workspace_id"], "external.loop.ping", subject=("loop", "1"), cause=e, source="rule")

    lyst.events.consume("test.echo", echo, types=["external.loop.*"])
    lyst.events.dispatch()
    root = lyst.events.external(owner, "northwind", "external.loop.ping", subject=("loop", "1"), data={}, dedupe_key=None)
    for _ in range(10):
        lyst.events.dispatch()
    chain = lyst.events.get(owner, "northwind", root["seq"])["chain"]
    pings = [e for e in chain if e["type"] == "external.loop.ping"]
    assert [e["depth"] for e in pings] == [0, 1, 2, 3, 4]
    assert all(e["correlation_id"] == root["id"] for e in chain)
    assert [p["causation_id"] for p in pings[1:]] == [p["id"] for p in pings[:-1]]
    stops = [e for e in chain if e["type"] == "event.chain_stopped"]
    assert len(stops) == 1 and stops[0]["data"]["limit"] == 4


def test_a_task_started_by_an_event_continues_its_chain(seeded):
    lyst, owner = seeded
    root = lyst.events.external(owner, "northwind", "external.ops.alert", subject=("alert", "a"), data={}, dedupe_key=None)
    tid = lyst.runtime.submit(owner, "northwind", "What was revenue last quarter?",
                              extra={"trigger": {"id": root["id"], "correlation_id": root["correlation_id"], "depth": 0}})
    lyst.runtime.drain()
    lyst.events.collect()
    done = [e for e in lyst.events.since("northwind", root["seq"]) if e["subject"]["id"] == tid and e["type"] == "task.completed"]
    assert done and done[0]["correlation_id"] == root["id"] and done[0]["causation_id"] == root["id"] and done[0]["depth"] == 1


def test_events_never_cross_workspaces_and_confidential_events_stay_with_admins(seeded):
    """Scenario K: a member of one workspace cannot read or subscribe to another's events."""
    lyst, owner = seeded
    other = lyst.guard.create_user("eve@else.test", "Eve")
    lyst.guard.create_workspace(other, "elsewhere", "Elsewhere")
    lyst.events.external(other, "elsewhere", "external.crm.secret", subject=("deal", "z"), data={}, dedupe_key=None)
    with pytest.raises(AccessDenied):
        lyst.events.list(owner, "elsewhere")
    with pytest.raises(AccessDenied):
        lyst.events.list(other, "northwind")
    foreign = lyst.events.since("elsewhere", 0)[0]
    assert not lyst.events.visible(owner, "northwind", foreign, "owner")
    with pytest.raises(KeyError):
        lyst.events.get(owner, "northwind", foreign["seq"])
    viewer = lyst.guard.create_user("vi@northwind.test", "Vi")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    conf = lyst.events.emit("northwind", "dataset.schema_changed", subject=("dataset", "payroll"), sensitivity="confidential")
    pub = lyst.events.emit("northwind", "dataset.schema_changed", subject=("dataset", "sales"))
    seen = {e["seq"] for e in lyst.events.list(viewer, "northwind", after=conf["seq"] - 1)["events"]}
    assert pub["seq"] in seen and conf["seq"] not in seen
    assert conf["seq"] in {e["seq"] for e in lyst.events.list(owner, "northwind", after=conf["seq"] - 1)["events"]}


def _sse(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        msg = dict(line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":") and ": " in line)
        if "data" in msg:
            out.append(msg)
    return out


def test_the_stream_replays_what_a_reconnecting_client_missed(seeded):
    """Scenario J: a client that disconnects and reconnects with Last-Event-ID gets every event it missed, once, in
    order; another workspace's stream is refused."""
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        start = c.get("/v1/workspaces/northwind/events", headers=h, params={"after": -1}).json()["next"]
        a = lyst.events.external(owner, "northwind", "external.x.one", subject=("x", "1"), data={}, dedupe_key=None)
        r = c.get("/v1/workspaces/northwind/events/stream", headers=h,
                  params={"after": start, "max_seconds": 1, "types": "external.*"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        msgs = [m for m in _sse(r.text) if m.get("event") != "end"]
        assert [int(m["id"]) for m in msgs] == [a["seq"]] and json.loads(msgs[0]["data"])["type"] == "external.x.one"
        # while disconnected, two more happen
        b = lyst.events.external(owner, "northwind", "external.x.two", subject=("x", "2"), data={}, dedupe_key=None)
        cc = lyst.events.external(owner, "northwind", "external.x.three", subject=("x", "3"), data={}, dedupe_key=None)
        r = c.get("/v1/workspaces/northwind/events/stream", headers=h | {"Last-Event-ID": msgs[-1]["id"]},
                  params={"max_seconds": 1, "types": "external.*"})
        again = [int(m["id"]) for m in _sse(r.text) if m.get("event") != "end"]
        assert again == [b["seq"], cc["seq"]]
        other = lyst.guard.create_user("eve@else.test", "Eve")
        lyst.guard.create_workspace(other, "elsewhere", "Elsewhere")
        assert c.get("/v1/workspaces/elsewhere/events/stream", headers=h, params={"max_seconds": 1}).status_code == 403
        # an integration event through the API: admins only, external.* only, deduplicated
        body = {"type": "external.crm.deal_closed", "subject": {"kind": "deal", "id": "d9"}, "dedupe_key": "deal-d9"}
        first = c.post("/v1/workspaces/northwind/events", headers=h, json=body)
        assert first.status_code == 201
        dup = c.post("/v1/workspaces/northwind/events", headers=h, json=body).json()
        assert dup["seq"] == first.json()["seq"] and dup["duplicate"] is True
        assert c.post("/v1/workspaces/northwind/events", headers=h, json=body | {"type": "dataset.fake"}).status_code == 400
        viewer = lyst.guard.create_user("vi@northwind.test", "Vi")
        lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
        hv = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer, 'test')}"}
        assert c.post("/v1/workspaces/northwind/events", headers=hv, json=body).status_code == 403
        detail = c.get(f"/v1/workspaces/northwind/events/{first.json()['seq']}", headers=h).json()
        assert detail["event"]["id"] == first.json()["id"] and detail["chain"][0]["id"] == first.json()["id"]


def test_the_stream_cursor_only_moves_forward(seeded):
    """Scenario H, at the transport: sequence numbers are strictly increasing per workspace, so a client that applies
    an event only when its seq is above the last applied one can never let a stale event overwrite a newer state."""
    lyst, owner = seeded
    start = lyst.events.list(owner, "northwind", after=-1)["next"]
    for i in range(5):
        lyst.events.external(owner, "northwind", "external.x.n", subject=("x", str(i)), data={"i": i}, dedupe_key=None)
    seqs = [e["seq"] for e in lyst.events.list(owner, "northwind", after=start)["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) == 5
    # a request with an older cursor than the last page never moves the returned cursor back
    assert lyst.events.list(owner, "northwind", after=seqs[-1])["next"] >= seqs[-1]


def test_the_envelope_schema_document_matches_the_model():
    path = Path(__file__).resolve().parents[1] / "docs" / "events" / "envelope.schema.json"
    assert json.loads(path.read_text()) == Event.model_json_schema(by_alias=True), \
        "regenerate docs/events/envelope.schema.json with scripts/event_schema.py"


def test_every_emitted_type_is_listed():
    assert {"dataset.schema_changed", "asset.health_changed", "event.chain_stopped"} <= set(ev_mod.types())
