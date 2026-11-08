# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Approvals in the chat: a Lyst approval is the AI SDK's own tool approval, answered with addToolApprovalResponse in
any client and applied only through runtime.decide, under its role checks."""

from __future__ import annotations

import json
import subprocess
import threading
import time

from fastapi.testclient import TestClient

from lyst import harness, tickets, uistream
from lyst.api import aisdk, create_app

from .test_uistream import AISDK, NODE, _snapshot, needs_aisdk, read_with_ai_sdk

Q = "What was revenue last quarter?"
AP = {"id": "ap_1", "action": "task.step", "summary": "Deliver the answer", "status": "pending"}


def _continue(message: dict, sse: str) -> dict:
    out = subprocess.run([NODE, str(AISDK / "continue_stream.mjs")], input=json.dumps({"message": message, "sse": sse}).encode(),
                         capture_output=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _sse(chunks) -> str:
    return "".join(uistream.sse(c) for c in chunks) + uistream.DONE


def test_an_approval_is_a_tool_approval_request():
    ts = uistream.TaskStream("t_1")
    first = ts.update(_snapshot(status="awaiting_approval"), [AP])
    ap = [c for c in first if c.get("toolCallId") == "approval:ap_1" or c.get("approvalId") == "ap_1"]
    assert [c["type"] for c in ap] == ["tool-input-available", "tool-approval-request"]
    assert ap[0]["toolName"] == "lyst_approval" and ap[0]["input"]["summary"] == "Deliver the answer"
    assert ts.update(_snapshot(status="awaiting_approval"), [AP]) == []  # unchanged: nothing new
    done = ts.update(_snapshot(status="running"), [{**AP, "status": "approved", "decided_by": "u_1"}])
    assert [c["type"] for c in done if "approvalId" in c or "toolCallId" in c] == [
        "tool-approval-response", "tool-output-available"]
    no = uistream.TaskStream("t_2").update(_snapshot(status="cancelled"), [{**AP, "status": "rejected"}])
    assert "tool-output-denied" in [c["type"] for c in no]


def test_answers_are_read_only_from_the_last_assistant_message():
    part = lambda state, approved=None: {"type": "dynamic-tool", "toolName": "lyst_approval", "toolCallId": "approval:a",  # noqa: E731
                                         "state": state, "approval": {"id": "a", **({} if approved is None else {"approved": approved})}}
    msgs = [{"role": "user", "parts": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "parts": [part("approval-responded", True), {**part("approval-responded", False),
                                                                                 "toolName": "other_tool"}]}]
    assert uistream.approval_responses(msgs) == [("a", True)]
    assert uistream.approval_responses([*msgs, {"role": "user", "parts": []}]) == []  # a new question, not an answer
    assert uistream.approval_responses([msgs[0], {"role": "assistant", "parts": [part("approval-requested")]}]) == []


@needs_aisdk
def test_the_ai_sdk_shows_the_approval_and_its_outcome():
    ts = uistream.TaskStream("t_1")
    chunks = ts.update(_snapshot(status="awaiting_approval"), [AP])
    msg = read_with_ai_sdk(_sse(chunks))["messages"][-1]
    tool = next(p for p in msg["parts"] if p["type"] == "dynamic-tool")
    assert tool["state"] == "approval-requested" and tool["approval"]["id"] == "ap_1"
    # what addToolApprovalResponse does to the part, then the engine's continuation of the same message
    tool.update(state="approval-responded", approval={"id": "ap_1", "approved": True})
    more = uistream.TaskStream("t_1").chunks(_snapshot(status="done", result={"answer": "Revenue was up."}),
                                             [{**AP, "status": "approved", "decided_by": "u_1"}])
    got = _continue(msg, _sse(more))
    assert got["error"] is None, got["error"]
    tools = [p for p in got["messages"][-1]["parts"] if p["type"] == "dynamic-tool" and p["toolName"] == "lyst_approval"]
    assert len(tools) == 1 and tools[0]["state"] == "output-available" and tools[0]["approval"]["approved"] is True


def _gate_deliver(lyst):
    lyst.stores.control.execute(
        "INSERT INTO harness_rules (id, workspace_id, kind, job_key, capability, signature, text, spec, status, "
        "source_failures) VALUES ('hr_p', 'northwind', 'permission', ?, 'deliver', 's', 'ask first', '{}', 'active', '[]')",
        [harness.job_key(Q)])


def _drain_soon(lyst, delay=0.2):
    threading.Thread(target=lambda: (time.sleep(delay), lyst.runtime.drain()), daemon=True).start()


def _chunks(text):
    return [json.loads(line[6:]) for line in text.split("\n\n") if line.startswith("data: {")]


def test_a_person_approves_in_the_chat_and_the_answer_follows(seeded, monkeypatch):
    monkeypatch.setattr(aisdk, "POLL", 0.05)
    monkeypatch.setattr(aisdk, "MAX_SECONDS", 1.5)  # the first stream ends while the task waits; the client resumes
    lyst, owner = seeded
    _gate_deliver(lyst)
    admin = lyst.guard.create_user("ana@northwind.test", "Ana")
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    h = lambda u: {"Authorization": f"Bearer {lyst.guard.issue_token(u, 'test')}"}  # noqa: E731
    user_msg = {"id": "m1", "role": "user", "parts": [{"type": "text", "text": Q}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        _drain_soon(lyst)
        first = c.post("/v1/workspaces/northwind/chat", headers=h(owner), json={"id": "c1", "messageId": "m1", "messages": [user_msg]})
        chunks = _chunks(first.text)
        req = next(x for x in chunks if x["type"] == "tool-approval-request")
        task_id = chunks[0]["messageId"]
        assert lyst.runtime.get(owner, task_id)["status"] == "awaiting_approval"
        answered = {"id": task_id, "role": "assistant", "parts": [
            {"type": "dynamic-tool", "toolName": "lyst_approval", "toolCallId": req["toolCallId"], "state": "approval-responded",
             "input": {}, "approval": {"id": req["approvalId"], "approved": True}}]}
        body = {"id": "c1", "messageId": task_id, "messages": [user_msg, answered]}
        # the answer is applied as the person who sent it, under the role checks: a viewer cannot approve
        assert c.post("/v1/workspaces/northwind/chat", headers=h(viewer), json=body).status_code == 403
        # and a chat ticket never decides anything, whoever holds it
        t = tickets.issue(admin.user_id, "northwind")["ticket"]
        assert c.post("/v1/workspaces/northwind/chat", headers={"Authorization": f"Ticket {t}"}, json=body).status_code == 403
        bad = json.loads(json.dumps(body))
        bad["messages"][1]["parts"][0]["approval"]["id"] = "ap_nope"
        assert c.post("/v1/workspaces/northwind/chat", headers=h(admin), json=bad).status_code == 404
        assert lyst.runtime.db.scalar("SELECT status FROM approvals WHERE id = ?", [req["approvalId"]]) == "pending"
        monkeypatch.setattr(aisdk, "MAX_SECONDS", 30)
        _drain_soon(lyst)
        second = c.post("/v1/workspaces/northwind/chat", headers=h(admin), json=body)
        assert second.status_code == 200 and second.text.endswith(uistream.DONE)
    ap = lyst.runtime.db.query("SELECT status, decided_by FROM approvals WHERE id = ?", [req["approvalId"]])[0]
    assert ap == {"status": "approved", "decided_by": admin.user_id}
    out = _chunks(second.text)
    assert "start" not in [x["type"] for x in out]  # the client's message continues: no new message, no second task
    assert not [x for x in out if x["type"] == "tool-input-available" and x["toolCallId"] == req["toolCallId"]]
    assert any(x["type"] == "tool-approval-response" and x["approved"] for x in out)
    assert any(x["type"] == "text-delta" for x in out) and out[-1] == {**out[-1], "type": "finish", "finishReason": "stop"}
    assert lyst.runtime.db.scalar("SELECT count(*) FROM tasks WHERE request->>'$.chat_id' = 'c1'") == 1
    if NODE is not None and (AISDK / "node_modules" / "ai").exists():
        got = _continue({**answered, "metadata": {}}, second.text)
        assert got["error"] is None, got["error"]
        last = got["messages"][-1]
        assert any(p["type"] == "text" for p in last["parts"])
        approvals = [p for p in last["parts"] if p.get("toolName") == "lyst_approval"]
        assert len(approvals) == 1 and approvals[0]["state"] == "output-available"  # shown once, now decided


def test_declining_in_the_chat_cancels_the_task(seeded, monkeypatch):
    monkeypatch.setattr(aisdk, "POLL", 0.05)
    monkeypatch.setattr(aisdk, "MAX_SECONDS", 1.5)
    lyst, owner = seeded
    _gate_deliver(lyst)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    user_msg = {"id": "m1", "role": "user", "parts": [{"type": "text", "text": Q}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        _drain_soon(lyst)
        chunks = _chunks(c.post("/v1/workspaces/northwind/chat", headers=h, json={"id": "c2", "messages": [user_msg]}).text)
        req = next(x for x in chunks if x["type"] == "tool-approval-request")
        answered = {"id": chunks[0]["messageId"], "role": "assistant", "parts": [
            {"type": "dynamic-tool", "toolName": "lyst_approval", "toolCallId": req["toolCallId"], "state": "approval-responded",
             "input": {}, "approval": {"id": req["approvalId"], "approved": False}}]}
        out = _chunks(c.post("/v1/workspaces/northwind/chat", headers=h, json={"id": "c2", "messages": [user_msg, answered]}).text)
    assert [x["type"] for x in out if x.get("toolCallId") == req["toolCallId"] or x.get("approvalId") == req["approvalId"]][-2:] == [
        "tool-approval-response", "tool-output-denied"]
    assert out[-1]["type"] == "abort" and lyst.runtime.get(owner, chunks[0]["messageId"])["status"] == "cancelled"
