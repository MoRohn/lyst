# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Python SDK (sdk/python) against a real engine app: files, questions, SQL, events, webhooks, errors."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.webhooks import sign

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
import lyst_client  # noqa: E402
from lyst_client import AuthError, Invalid, Lyst, NotFound, RateLimited, verify_signature  # noqa: E402


@pytest.fixture
def sdk(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner, "sdk")
    with TestClient(create_app(lyst, start_workers=True)) as http:
        yield Lyst("http://testserver", token, http=http), lyst


def test_files_questions_sql_and_events(sdk):
    client, lyst = sdk
    assert client.me()["workspaces"][0]["id"] == "northwind"
    ws = client.workspace("northwind")
    assert "xlsx" in ws.files.formats()["extensions"]
    plan = ws.files.inspect(b"region;target\nEMEA;10\nAPAC;8\n", filename="targets.csv")
    assert plan["delimiter"] == ";" and plan["tables"][0]["rows"] == 2
    out = ws.files.commit(plan["id"], tables=[{"key": plan["tables"][0]["key"], "name": "targets"}])
    assert out["datasets"][0]["dataset"] == "targets"
    r = ws.sql("SELECT sum(target) AS t FROM targets")
    assert r.records() == [{"t": 18}] and r.to_pandas()["t"][0] == 18
    start = ws.events.list(after=-1)["next"]
    task = ws.ask("What was operating margin last quarter?", timeout=120)
    assert task["status"] == "done"
    types = [e["type"] for e in ws.events.list(after=start)["events"]]
    assert "task.created" in types and "task.completed" in types
    # a retried submit with the same key is the same task
    k = "same-key-1"
    assert ws.submit("What was revenue last quarter?", idempotency_key=k) == ws.submit("What was revenue last quarter?", idempotency_key=k)


def test_stream_yields_new_events(sdk):
    client, lyst = sdk
    ws = client.workspace("northwind")
    got = []

    def later():
        time.sleep(0.6)
        ws.files.load(b"a,b\n1,2\n", filename="later.csv")

    th = threading.Thread(target=later)
    th.start()
    for e in ws.events.stream(types=["dataset.*"], wait=2, stop_after=6):
        got.append(e)
        break
    th.join()
    assert got and got[0]["subject"]["id"] == "later"


def test_typed_errors_and_version_warning(sdk):
    client, _ = sdk
    ws = client.workspace("northwind")
    with pytest.raises(NotFound):
        client.task("t_missing")
    with pytest.raises(Invalid):
        ws.files.inspect(b"\xd0\xcf\x11\xe0" + b"\0" * 64, filename="old.xls")
    with pytest.raises(AuthError):
        Lyst("http://testserver", "wrong", http=client.http).me()
    with pytest.raises(AuthError):
        client.workspace("elsewhere").datasets()


def test_retries_honour_retry_after_and_warn_on_newer_engine(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"detail": "slow down"})
        return httpx.Response(200, headers={"lyst-api-version": "2"}, json={"status": "ok"})

    c = Lyst("http://x", "t", http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)))
    with pytest.warns(UserWarning, match="version 2"):
        assert c.health() == {"status": "ok"}
    assert len(calls) == 2 and calls[0].headers["authorization"] == "Bearer t"
    always = Lyst("http://x", http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "0"}, json={"detail": "no"}))), retries=1)
    with pytest.raises(RateLimited) as e:
        always.health()
    assert e.value.retry_after == 0.0
    # a POST is not retried unless it is idempotent
    posts = []
    flaky = Lyst("http://x", http=httpx.Client(base_url="http://x", transport=httpx.MockTransport(
        lambda r: posts.append(r) or httpx.Response(503, json={"detail": "busy"}))), retries=2)
    with pytest.raises(lyst_client.ServerError):
        flaky.request("POST", "/v1/anything", json={})
    assert len(posts) == 1


def test_verify_signature_matches_the_engine():
    body = b'{"type":"ping"}'
    header = sign("whsec_x", body)
    assert verify_signature("whsec_x", header, body)
    assert not verify_signature("whsec_y", header, body)
    assert not verify_signature("whsec_x", header, body + b" ")
    old = sign("whsec_x", body, t=int(time.time()) - 3600)
    assert not verify_signature("whsec_x", old, body)
    assert not verify_signature("whsec_x", "garbage", body)
