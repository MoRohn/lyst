# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The event model and signed webhooks: derived from the audit log and tasks, permission-filtered, retried."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.security import AccessDenied
from lyst.webhooks import backoff, check_target, sign

from .conftest import ask


class Receiver:
    def __init__(self, fail: int = 0, status: int = 500) -> None:
        self.got: list[httpx.Request] = []
        self.fail = fail
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.got.append(request)
        if self.fail:
            self.fail -= 1
            return httpx.Response(self.status, text="not now")
        return httpx.Response(204)

    def events(self) -> list[dict]:
        return [json.loads(r.content) for r in self.got]


def _due_now(lyst) -> None:
    lyst.stores.control.execute("UPDATE webhook_deliveries SET next_attempt_at = now() WHERE status = 'pending'")


def test_events_come_from_audit_and_tasks(seeded):
    lyst, owner = seeded
    lyst.events.collect()
    first = lyst.events.list(owner, "northwind")["next"]
    lyst.data.ingest(owner, "northwind", "targets", b"region,target\nEMEA,10\nAPAC,8\n", filename="targets.csv")
    t = ask(lyst, owner, "What was operating margin last quarter?")
    page = lyst.events.list(owner, "northwind", after=first)
    by_type = {e["type"]: e for e in page["events"]}
    v = by_type["dataset.version_created"]
    assert v["subject"] == {"kind": "dataset", "id": "targets"} and v["data"]["rows"] == 2 and v["schema"] == 2
    assert by_type["task.created"]["subject"]["id"] == t["id"]
    done = by_type["task.completed"]
    assert done["id"] == f"t:{t['id']}:done" and done["data"]["created_by"] == owner.user_id
    assert "answer" not in json.dumps(done["data"])
    # the collector is idempotent and the cursor moves on
    lyst.events.collect()
    again = lyst.events.list(owner, "northwind", after=page["next"])
    assert [e for e in again["events"] if e["type"] == "task.completed"] == []
    only = lyst.events.list(owner, "northwind", after=first, types=["dataset.*"])["events"]
    assert {e["type"] for e in only} == {"dataset.version_created"}
    # in-process subscribers see each new event once
    seen = []
    lyst.events.subscribers.append(seen.append)
    lyst.data.ingest(owner, "northwind", "targets", b"region,target\nEMEA,11\nAPAC,8\n", filename="targets.csv")
    lyst.events.collect()
    lyst.events.collect()
    assert [e["type"] for e in seen] == ["dataset.version_created"]


def test_members_see_their_own_tasks_and_shared_changes_only(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    ask(lyst, owner, "What was operating margin last quarter?")
    mine = ask(lyst, viewer, "What was revenue last quarter?")
    types = [(e["type"], e["subject"]["id"]) for e in lyst.events.list(viewer, "northwind", limit=500)["events"]]
    tasks = {sid for t, sid in types if t.startswith("task.")}
    assert tasks == {mine["id"]}
    assert not any(t == "member.added" for t, _ in types)
    assert any(t == "member.added" for t, _ in [(e["type"], 0) for e in lyst.events.list(owner, "northwind", limit=500)["events"]])
    outsider = lyst.guard.create_user("eve@elsewhere.test", "Eve")
    with pytest.raises(AccessDenied):
        lyst.events.list(outsider, "northwind")


def test_webhooks_are_signed_retried_dead_lettered_and_replayed(seeded, monkeypatch):
    monkeypatch.setenv("LYST_WEBHOOK_ALLOW_PRIVATE", "1")
    lyst, owner = seeded
    rx = Receiver(fail=1)
    lyst.webhooks.client = httpx.Client(transport=httpx.MockTransport(rx))
    sub = lyst.webhooks.create(owner, "northwind", "https://hooks.example.test/lyst", types_=["dataset.*"])
    secret = sub["secret"]
    assert secret.startswith("whsec_") and "secret" not in lyst.webhooks.list(owner, "northwind")[0]
    lyst.data.ingest(owner, "northwind", "targets", b"region,target\nEMEA,10\n", filename="targets.csv")
    assert lyst.webhooks.run() == {"sent": 0, "failed": 1}
    d = lyst.webhooks.deliveries(owner, "northwind", sub["id"])[0]
    assert d["status"] == "pending" and d["attempts"] == 1 and d["last_status"] == 500
    assert lyst.webhooks.run() == {"sent": 0, "failed": 0}  # not due yet: backoff
    _due_now(lyst)
    assert lyst.webhooks.run()["sent"] == 1
    req = rx.got[-1]
    body = req.content
    t, mac = (kv.split("=", 1)[1] for kv in req.headers["lyst-signature"].split(","))
    assert hmac.compare_digest(mac, hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest())
    assert req.headers["lyst-event"] == "dataset.version_created"
    assert req.headers["lyst-delivery"] == d["id"]  # stable across retries
    assert json.loads(body)["subject"]["id"] == "targets"
    # a dead receiver: eight attempts, then dead; redeliver brings it back
    rx.fail, rx.status = 100, 503
    lyst.data.ingest(owner, "northwind", "targets", b"region,target\nEMEA,12\n", filename="targets.csv")
    for _ in range(8):
        _due_now(lyst)
        lyst.webhooks.run()
    dead = lyst.webhooks.deliveries(owner, "northwind", sub["id"], status="dead")
    assert len(dead) == 1 and dead[0]["attempts"] == 8
    rx.fail = 0
    assert lyst.webhooks.redeliver(owner, "northwind", dead[0]["id"])["status"] == "delivered"
    # events outside the subscription's types are not sent; ping works and is outside the queue
    n = len(rx.got)
    lyst.semantic  # noqa: B018
    ask(lyst, owner, "What was operating margin last quarter?")
    lyst.webhooks.run()
    assert len(rx.got) == n
    assert lyst.webhooks.ping(owner, "northwind", sub["id"])["ok"]
    rotated = lyst.webhooks.update(owner, "northwind", sub["id"], rotate_secret=True)
    assert rotated["secret"] != secret


def test_receivers_that_keep_refusing_pause_the_subscription(seeded, monkeypatch):
    monkeypatch.setenv("LYST_WEBHOOK_ALLOW_PRIVATE", "1")
    monkeypatch.setattr("lyst.webhooks.PAUSE_AFTER_DEAD", 2)
    lyst, owner = seeded
    rx = Receiver(fail=1000, status=410)
    lyst.webhooks.client = httpx.Client(transport=httpx.MockTransport(rx))
    sub = lyst.webhooks.create(owner, "northwind", "https://gone.example.test/", types_=["dataset.version_created"])
    for i in range(2):
        lyst.data.ingest(owner, "northwind", "targets", f"region,target\nEMEA,{i}\n".encode(), filename="targets.csv")
    for _ in range(3):  # a 410 is dead after three attempts, not eight
        _due_now(lyst)
        lyst.webhooks.run()
    s = lyst.webhooks.list(owner, "northwind")[0]
    assert s["active"] is False and "paused after 2" in s["disabled_reason"]
    assert lyst.webhooks.update(owner, "northwind", sub["id"], active=True)["active"] is True


def test_webhook_targets_are_checked(monkeypatch):
    monkeypatch.delenv("LYST_WEBHOOK_ALLOW_PRIVATE", raising=False)
    for bad, why in [("http://example.com/x", "https"), ("https://127.0.0.1/x", "private"), ("https://10.1.2.3/", "private"),
                     ("https://169.254.169.254/latest", "private"), ("https://user:pw@example.com/", "credentials"),
                     ("https://[::1]/", "private")]:
        with pytest.raises(ValueError, match=why):
            check_target(bad)
    assert backoff(1) == 30 and backoff(2) == 60 and backoff(3) == 120 and backoff(30) == 6 * 3600
    assert sign("s", b"{}", 1).startswith("t=1,v1=")


def test_webhook_and_event_api(seeded, monkeypatch):
    monkeypatch.setenv("LYST_WEBHOOK_ALLOW_PRIVATE", "1")
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    hv = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/events/types", headers=h)
        assert r.status_code == 200 and "dataset.version_created" in r.json()["types"]
        assert r.headers["lyst-api-version"] == "1"
        r = c.post("/v1/workspaces/northwind/webhooks", headers=h, json={"url": "https://hooks.example.test/", "types": ["nope.*"]})
        assert r.status_code == 400
        assert c.post("/v1/workspaces/northwind/webhooks", headers=hv, json={"url": "https://hooks.example.test/"}).status_code == 403
        r = c.post("/v1/workspaces/northwind/webhooks", headers=h, json={"url": "https://hooks.example.test/", "types": ["task.*"]})
        assert r.status_code == 201 and r.json()["secret"]
        sid = r.json()["id"]
        assert c.get(f"/v1/workspaces/northwind/webhooks/{sid}/deliveries", headers=h).json() == []
        first = c.get("/v1/workspaces/northwind/events", headers=h).json()["next"]
        c.post("/v1/workspaces/northwind/tasks", headers=h, json={"question": "What was revenue last quarter?"})
        page = c.get("/v1/workspaces/northwind/events", headers=h, params={"after": first, "types": "task.*", "wait": 2}).json()
        assert page["events"] and page["events"][0]["type"] == "task.created" and page["next"] > first
        assert c.delete(f"/v1/workspaces/northwind/webhooks/{sid}", headers=h).json() == {"deleted": True}
