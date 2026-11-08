# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's work as an AI SDK UI message stream (``lyst.uistream``, ``api/aisdk.py``).

The protocol tests read Lyst's stream with the real ``ai`` package (``tests/aisdk``: ``parseJsonEventStream`` with the
SDK's own chunk schema, then ``readUIMessageStream``, as ``useChat`` does). They run when that package is installed
(``npm ci --prefix tests/aisdk``; CI installs it) and are skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyst import uistream
from lyst.api import create_app

AISDK = Path(__file__).parent / "aisdk"
NODE = shutil.which("node")
HAVE_AISDK = NODE is not None and (AISDK / "node_modules" / "ai" / "package.json").exists()
needs_aisdk = pytest.mark.skipif(not HAVE_AISDK, reason="the ai package is not installed (npm ci --prefix tests/aisdk)")


def read_with_ai_sdk(sse: str) -> dict:
    out = subprocess.run([NODE, str(AISDK / "read_stream.mjs")], input=sse.encode(), capture_output=True,
                         timeout=60, check=True)
    return json.loads(out.stdout)


def _snapshot(status="running", steps=(), result=None, **kw):
    return {"id": "t_1", "workspace_id": "northwind", "title": "Revenue last quarter", "status": status,
            "steps": list(steps), "result": result, "lifecycle": {"state": status}, **kw}


STEP_RUNNING = {"seq": 1, "phase": "understand", "capability": "understand", "args": {"q": "revenue"}, "status": "running"}
STEP_DONE = {**STEP_RUNNING, "status": "done", "finished_at": "2026-11-08T12:00:00+00:00"}
STEP2_FAILED = {"seq": 2, "phase": "query", "capability": "metric_query", "args": None, "status": "failed", "error": "boom"}
RESULT = {"answer": "Revenue was $1.2m last quarter.", "status": "answered", "mode": "analysis",
          "confidence": {"level": "verified", "score": 0.93},
          "supporting": {"sources": [{"dataset": "sales", "version": 3}, {"dataset": "sales", "version": 3}]},
          "charts": [{"title": "Revenue by month", "kind": "line"}]}


def _types(chunks):
    return [c["type"] for c in chunks]


# -- the diff ------------------------------------------------------------------------------------------------------

def test_a_task_becomes_a_message_step_by_step():
    ts = uistream.TaskStream("t_1")
    first = ts.update(_snapshot(steps=[STEP_RUNNING]))
    assert _types(first) == ["start", "data-lyst-status", "start-step", "tool-input-available"]
    assert first[0]["messageId"] == "t_1"
    assert first[3] == {"type": "tool-input-available", "toolCallId": "t_1:1", "toolName": "understand",
                        "input": {"q": "revenue"}, "dynamic": True, "title": "understand"}
    assert ts.update(_snapshot(steps=[STEP_RUNNING])) == []  # nothing changed, nothing sent
    second = ts.update(_snapshot(steps=[STEP_DONE]))
    assert _types(second) == ["tool-output-available", "finish-step"]
    assert second[0]["output"]["status"] == "done"
    end = ts.update(_snapshot(status="done", steps=[STEP_DONE], result=RESULT, outcome="answered"), [])
    assert _types(end) == ["data-lyst-status", "text-start", "text-delta", "text-end", "source-document",
                           "data-lyst-chart", "finish"]
    assert end[2]["delta"] == RESULT["answer"]
    assert end[4] == {"type": "source-document", "sourceId": "sales@3", "mediaType": uistream.DATASET_MEDIA_TYPE,
                      "title": "sales v3"}  # one per dataset version, however often it was read
    assert end[-1]["finishReason"] == "stop"
    assert end[-1]["messageMetadata"]["confidence"]["level"] == "verified"
    assert end[-1]["messageMetadata"]["outcome"] == "answered"
    assert ts.finished and ts.update(_snapshot(status="done", result=RESULT)) == []


def test_a_fresh_stream_replays_the_whole_task_once():
    snap = _snapshot(status="done", steps=[STEP_DONE], result=RESULT)
    chunks = uistream.TaskStream("t_1").chunks(snap)
    assert _types(chunks)[:6] == ["start", "data-lyst-status", "start-step", "tool-input-available",
                                  "tool-output-available", "finish-step"]
    assert _types(chunks).count("start") == 1 and chunks[-1]["type"] == "finish"


def test_failures_cancellations_and_approvals():
    ts = uistream.TaskStream("t_1")
    ts.update(_snapshot(steps=[STEP_DONE, {**STEP2_FAILED, "status": "running"}]))
    pending = ts.update(_snapshot(status="awaiting_approval", steps=[STEP_DONE, {**STEP2_FAILED, "status": "running"}]),
                        [{"id": "ap_1", "action": "schedule.create", "summary": "Run weekly", "status": "pending"}])
    ap = next(c for c in pending if c["type"] == "data-lyst-approval")
    assert ap["id"] == "approval:ap_1" and ap["data"]["decide"] == "/v1/approvals/ap_1"
    assert ap["data"]["status"] == "pending"
    failed = ts.update(_snapshot(status="failed", steps=[STEP_DONE, STEP2_FAILED], error="no data"),
                       [{"id": "ap_1", "action": "schedule.create", "summary": "Run weekly", "status": "rejected"}])
    assert _types(failed) == ["data-lyst-status", "tool-output-error", "finish-step", "data-lyst-approval", "error", "finish"]
    assert failed[1]["errorText"] == "boom" and failed[4]["errorText"] == "no data"
    assert failed[-1]["finishReason"] == "error"
    assert _types(uistream.TaskStream("t_2").chunks(_snapshot(status="cancelled")))[-1] == "abort"


def test_the_question_is_the_last_user_message():
    msgs = [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "old"}]},
            {"id": "m2", "role": "assistant", "parts": [{"type": "text", "text": "reply"}]},
            {"id": "m3", "role": "user", "parts": [{"type": "text", "text": "What was"}, {"type": "file", "url": "x"},
                                                   {"type": "text", "text": "revenue?"}]}]
    assert uistream.question_from(msgs) == ("What was revenue?", "m3")
    assert uistream.question_from([{"id": "a", "role": "assistant", "parts": []}]) == (None, None)
    assert uistream.question_from([{"id": "u", "role": "user", "parts": [{"type": "file", "url": "x"}]}]) == (None, "u")


def test_sse_framing():
    assert uistream.sse({"type": "finish-step"}) == 'data: {"type":"finish-step"}\n\n'
    assert uistream.DONE == "data: [DONE]\n\n"
    assert uistream.HEADERS["x-vercel-ai-ui-message-stream"] == "v1"


# -- the real AI SDK reads it --------------------------------------------------------------------------------------

@needs_aisdk
def test_the_ai_sdk_reads_every_chunk_and_assembles_the_answer():
    ts = uistream.TaskStream("t_1")
    snaps = [_snapshot(steps=[STEP_RUNNING]), _snapshot(steps=[STEP_DONE, {**STEP2_FAILED, "status": "running"}]),
             _snapshot(status="done", steps=[STEP_DONE, {**STEP2_FAILED, "status": "done"}], result=RESULT)]
    approvals = [{"id": "ap_1", "action": "schedule.create", "summary": "Run weekly", "status": "approved"}]
    sse = "".join(uistream.sse(c) for s in snaps for c in ts.update(s, approvals)) + uistream.DONE
    got = read_with_ai_sdk(sse)
    assert got["error"] is None
    msg = got["messages"][-1]
    assert msg["id"] == "t_1" and msg["role"] == "assistant"
    assert msg["metadata"]["confidence"]["level"] == "verified"
    kinds = [p["type"] for p in msg["parts"]]
    assert kinds.count("step-start") == 2
    tools = [p for p in msg["parts"] if p["type"] == "dynamic-tool"]
    assert [(t["toolName"], t["state"]) for t in tools] == [("understand", "output-available"),
                                                            ("metric_query", "output-available")]
    assert tools[0]["input"] == {"q": "revenue"}
    text = [p for p in msg["parts"] if p["type"] == "text"]
    assert text == [{"type": "text", "text": RESULT["answer"], "state": "done"}]
    assert any(p["type"] == "source-document" and p["sourceId"] == "sales@3" for p in msg["parts"])
    # data parts with an id are reconciled in place: one status part, however often the status changed
    assert kinds.count("data-lyst-status") == 1 and kinds.count("data-lyst-approval") == 1
    assert next(p for p in msg["parts"] if p["type"] == "data-lyst-chart")["data"]["title"] == "Revenue by month"


@needs_aisdk
def test_the_ai_sdk_reads_a_failed_run():
    ts = uistream.TaskStream("t_9")
    sse = "".join(uistream.sse(c) for c in ts.chunks(_snapshot(status="failed", steps=[STEP2_FAILED], error="no data")))
    got = read_with_ai_sdk(sse + uistream.DONE)
    tool = next(p for p in got["messages"][-1]["parts"] if p["type"] == "dynamic-tool")
    assert tool["state"] == "output-error" and tool["errorText"] == "boom"
    assert "error" in got["chunks"]


# -- the API -------------------------------------------------------------------------------------------------------

def _drain_soon(lyst):
    def run():
        time.sleep(0.3)
        lyst.runtime.drain()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_chat_endpoint_streams_a_governed_answer(seeded, monkeypatch):
    from lyst.api import aisdk

    monkeypatch.setattr(aisdk, "POLL", 0.05)
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    body = {"id": "chat_1", "trigger": "submit-message", "messageId": None,
            "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "What was revenue last quarter?"}]}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        t = _drain_soon(lyst)
        r = c.post("/v1/workspaces/northwind/chat", headers=h, json=body)
        t.join()
        assert r.status_code == 200
        assert r.headers["x-vercel-ai-ui-message-stream"] == "v1"
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.text.endswith(uistream.DONE)
        chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
        assert chunks[0]["type"] == "start" and chunks[-1]["type"] in ("finish", "abort")
        task_id = chunks[0]["messageId"]
        task = lyst.runtime.get(owner, task_id)
        assert task["request"]["chat_id"] == "chat_1" and task["request"]["channel"] == "ai-sdk"
        assert sum(1 for x in chunks if x["type"] == "tool-input-available") == len(task["steps"])
        if (task["result"] or {}).get("answer"):
            assert any(x["type"] == "text-delta" and x["delta"] == task["result"]["answer"] for x in chunks)
        # a retried request for the same user message streams the same task, it does not run the work again
        again = c.post("/v1/workspaces/northwind/chat", headers=h, json=body)
        assert json.loads(again.text.split("\n", 1)[0][6:])["messageId"] == task_id
        # nothing running for this chat: resume answers 204, as the AI SDK expects
        assert c.get("/v1/workspaces/northwind/chat/chat_1/stream", headers=h).status_code == 204
        if HAVE_AISDK:
            got = read_with_ai_sdk(r.text)
            assert got["error"] is None and got["messages"][-1]["id"] == task_id


def test_chat_endpoint_checks_the_person(seeded):
    lyst, owner = seeded
    outsider = lyst.guard.create_user("eve@elsewhere.test", "Eve")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(outsider, 'test')}"}
    ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    body = {"id": "c", "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": "revenue?"}]}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        assert c.post("/v1/workspaces/northwind/chat", headers=h, json=body).status_code == 403
        assert c.get("/v1/workspaces/northwind/chat/c/stream", headers=h).status_code == 403
        empty = {"id": "c", "messages": [{"id": "u", "role": "user", "parts": [{"type": "file", "url": "x"}]}]}
        assert c.post("/v1/workspaces/northwind/chat", headers=ho, json=empty).status_code == 422
