# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The answer as the model writes it: a streamed gateway call, a figure-checked draft (``lyst.live``) and the draft in
the AI SDK stream, removed unless it is exactly the stored answer."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst import live, uistream
from lyst.analyst.engine import _figures, _unbacked
from lyst.api import aisdk, create_app
from lyst.llm import LLMUnavailable
from lyst.llm.gateway import Exhausted, Gateway, read_stream
from lyst.llm.policy import AdmissionLimits, PoolLimits

from .test_uistream import HAVE_AISDK, needs_aisdk, read_with_ai_sdk


def _limits():
    return AdmissionLimits(max_inflight=12, interactive_reserve=4, max_tokens=1_000_000, per_tenant_tokens=250_000,
                           pools={"interactive": PoolLimits(8, 8, 64, 0.2), "background": PoolLimits(3, 3, 32, 0.2)})


def _sse(*pieces, usage=None, finish="stop", done=True, error=None):
    lines = [": OPENROUTER PROCESSING", ""]
    for p in pieces:
        lines += ["data: " + json.dumps({"model": "a/one", "choices": [{"delta": {"content": p}}]}), ""]
    lines += ["data: " + json.dumps({"choices": [{"delta": {"reasoning": "secret"}}]}), ""]
    if error:
        lines += ["data: " + json.dumps({"error": error}), ""]
    elif finish:
        lines += ["data: " + json.dumps({"provider": "Stub", "choices": [{"delta": {}, "finish_reason": finish}],
                                         "usage": usage or {"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.0002}}), ""]
    if done:
        lines += ["data: [DONE]", ""]
    return "\n".join(lines).encode()


SSE = {"content-type": "text/event-stream"}


def _gw(handler):
    calls = []
    g = Gateway(provider="openrouter", base_url="https://openrouter.test/api/v1", api_key="sk-or-test",
                http=httpx.Client(transport=httpx.MockTransport(handler)), limits=_limits(), sleep=lambda s: None,
                on_call=calls.append)
    return g, calls


# -- the gateway streams --------------------------------------------------------------------------------------------

def test_a_streamed_call_passes_text_on_and_returns_the_same_result():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=_sse("Revenue ", "rose."), headers=SSE)

    g, calls = _gw(handler)
    got: list = []
    r = g.complete([{"role": "user", "content": "hi"}], models=["a/one"], on_delta=got.append)
    assert got == ["Revenue ", "rose."]  # reasoning is never passed on
    assert r.text == "Revenue rose." and r.provider == "Stub" and r.output_tokens == 3 and r.cost_usd == 0.0002
    assert seen[0]["stream"] is True and seen[0]["usage"] == {"include": True}
    assert [c.outcome for c in calls] == ["ok"]
    # JSON is never streamed: the whole object is checked before anyone sees it
    g2, _ = _gw(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    g2.complete([{"role": "user", "content": "hi"}], models=["a/one"], json_mode=True, on_delta=got.append)
    assert got == ["Revenue ", "rose."]


def test_a_retry_after_text_was_shown_says_discard_it_first():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:  # the provider fails mid-reply
            return httpx.Response(200, content=_sse("Half a ", error={"code": 502, "message": "upstream"}), headers=SSE)
        if n["i"] == 2:  # the connection drops mid-reply
            return httpx.Response(200, content=_sse("Another ", finish=None, done=False), headers=SSE)
        return httpx.Response(200, content=_sse("Whole reply."), headers=SSE)

    g, calls = _gw(handler)
    got: list = []
    r = g.complete([{"role": "user", "content": "hi"}], models=["a/one"], on_delta=got.append)
    assert got == ["Half a ", None, "Another ", None, "Whole reply."] and r.text == "Whole reply."
    assert [c.outcome for c in calls] == ["upstream_error", "network", "ok"]
    g3, _ = _gw(lambda req: httpx.Response(503, json={"error": "down"}))
    with pytest.raises(Exhausted):
        g3.complete([{"role": "user", "content": "hi"}], models=["a/one"], on_delta=got.append)


def test_a_server_that_ignores_stream_still_works():
    g, calls = _gw(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "All of it."}}],
                                                         "usage": {"completion_tokens": 3}}))
    got: list = []
    r = g.complete([{"role": "user", "content": "hi"}], models=["a/one"], on_delta=got.append)
    assert got == ["All of it."] and r.text == "All of it." and r.output_tokens == 3


def test_read_stream_assembles_a_completion():
    got: list = []
    out = read_stream(_sse("a", "b").decode().splitlines(), got.append)
    assert out["choices"][0] == {"message": {"content": "ab"}, "finish_reason": "stop"} and out["provider"] == "Stub"
    with pytest.raises(ValueError):
        read_stream(_sse("a", finish=None, done=False).decode().splitlines(), got.append)


# -- the figure-checked draft ---------------------------------------------------------------------------------------

def _guard(task_id, allowed, drafts):
    return live.DraftGuard(task_id, _figures, lambda v: not _unbacked([v], allowed), drafts=drafts)


def _feed(guard, text):
    for i in range(0, len(text), 3):
        guard(text[i:i + 3])


def test_the_draft_shows_only_backed_figures():
    d = live.Drafts()
    g = _guard("t", [25_410_000.0, 0.013], d)
    _feed(g, "Revenue was $25.41 million in Q3, up 1.3% on the quarter, and then it grew by 7.7 million more after that.")
    text = d.get("t")["text"]
    assert "25.41 million" in text and "1.3%" in text  # backed, judged with their scale and unit
    assert "7.7" not in text and g.stopped  # the first unbacked figure stops the draft for good
    g.finish(None)  # the analyst kept its computed answer: the draft is withdrawn at once
    assert d.get("t")["text"] == "" and d.get("t")["epoch"] == 1


def test_the_draft_holds_back_a_figure_until_its_scale_arrives():
    d = live.Drafts()
    g = _guard("t", [2_690_000.0], d)
    _feed(g, "Sales reached 2.69")
    assert "2.69" not in (d.get("t") or {"text": ""})["text"]
    _feed(g, " million in the third quarter overall")
    assert "2.69 million" in d.get("t")["text"] and not g.stopped
    g(None)  # the gateway retries: start over
    assert d.get("t")["text"] == "" and d.get("t")["epoch"] == 1
    g.finish("Sales reached 2.69 million.")
    assert d.get("t") == {"epoch": 1, "text": "Sales reached 2.69 million.", "state": "written"}


def test_a_guard_without_a_task_does_nothing():
    d = live.Drafts()
    g = _guard(None, [], d)
    _feed(g, "anything at all here 99 times")
    g.finish("x")
    assert d.get("None") is None and not d._items


def test_drafts_expire():
    now = [0.0]
    d = live.Drafts(clock=lambda: now[0])
    d.set("a", "hello")
    now[0] = live.TTL + 1
    d.set("b", "x")
    assert d.get("a") is None and d.get("b")


# -- the draft in the AI SDK stream ---------------------------------------------------------------------------------

STEP = {"seq": 7, "phase": "deliver", "capability": "deliver", "args": {}, "status": "running"}
LEARN = {"seq": 8, "phase": "learn", "capability": "learn", "args": {}, "status": "done"}


def _task(status="running", steps=(STEP,), answer=None):
    return {"id": "t_1", "workspace_id": "w", "status": status, "steps": list(steps), "lifecycle": {"state": status},
            "result": {"answer": answer} if answer is not None else None}


def _run(snaps):
    ts = uistream.TaskStream("t_1")
    chunks = [c for task, draft in snaps for c in ts.update(task, [], draft)]
    return chunks, "".join(uistream.sse(c) for c in chunks) + uistream.DONE


def test_a_draft_that_is_the_answer_stays_and_is_not_sent_twice():
    final = "Revenue rose to 25.41 million."
    chunks, sse = _run([
        (_task(), {"epoch": 0, "text": "Revenue rose", "state": "writing"}),
        (_task(steps=(STEP, LEARN)), {"epoch": 0, "text": "Revenue rose to", "state": "writing"}),
        (_task("done", (STEP | {"status": "done"}, LEARN), final), {"epoch": 0, "text": final, "state": "written"}),
    ])
    types = [c["type"] for c in chunks]
    # the learn step waited behind the draft, so the draft stayed the newest step
    assert types.index("text-start") < types.index("text-end") < [i for i, c in enumerate(chunks)
                                                                  if c.get("toolName") == "learn"][0]
    assert sum(1 for c in chunks if c["type"] == "text-start") == 1 and "reset-step" not in types
    if HAVE_AISDK:
        msg = read_with_ai_sdk(sse)["messages"][-1]
        assert [p["text"] for p in msg["parts"] if p["type"] == "text"] == [final]


def test_a_draft_that_is_not_the_answer_is_removed():
    chunks, sse = _run([
        (_task(), {"epoch": 0, "text": "The model said something", "state": "writing"}),
        (_task("done", (STEP | {"status": "done"},), "The computed answer."), {"epoch": 0, "text": "The model said "
                                                                                "something else", "state": "written"}),
    ])
    assert "reset-step" in [c["type"] for c in chunks]
    if HAVE_AISDK:
        msg = read_with_ai_sdk(sse)["messages"][-1]
        assert [p["text"] for p in msg["parts"] if p["type"] == "text"] == ["The computed answer."]
        assert [p["toolName"] for p in msg["parts"] if p["type"] == "dynamic-tool"] == ["deliver"]


@needs_aisdk
def test_a_new_epoch_removes_what_was_shown():
    chunks, sse = _run([
        (_task(), {"epoch": 0, "text": "First try", "state": "writing"}),
        (_task(), {"epoch": 1, "text": "", "state": "writing"}),
        (_task(), {"epoch": 2, "text": "Second", "state": "writing"}),
        (_task("done", (STEP | {"status": "done"},), "Second try."), {"epoch": 2, "text": "Second try.", "state": "written"}),
    ])
    assert [c["type"] for c in chunks].count("reset-step") == 1
    msg = read_with_ai_sdk(sse)["messages"][-1]
    assert [p["text"] for p in msg["parts"] if p["type"] == "text"] == ["Second try."]


# -- end to end: a model's wording streams through the real engine ---------------------------------------------------

def test_the_chat_stream_shows_the_answer_as_the_model_writes_it(seeded, monkeypatch):
    lyst, owner = seeded
    llm = lyst.services.llm
    monkeypatch.setattr(aisdk, "POLL", 0.02)
    monkeypatch.setattr(llm, "allowed_for", lambda ws: True)

    def no_json(*a, **k):
        raise LLMUnavailable("no model for planning in this test")

    def chat(ws, messages, *, purpose, on_delta=None, **k):
        text = "In short: " + json.loads(messages[-1]["content"])["answer"] if purpose == "deliver" else ""
        for word in text.split(" "):
            if on_delta:
                on_delta(word + " ")
                time.sleep(0.01)
        return text

    monkeypatch.setattr(llm, "chat_json", no_json)
    monkeypatch.setattr(llm, "chat", chat)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    body = {"id": "c_live", "messages": [{"id": "u", "role": "user", "parts": [{"type": "text",
                                                                               "text": "Why did revenue change last quarter?"}]}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        threading.Thread(target=lambda: (time.sleep(0.2), lyst.runtime.drain()), daemon=True).start()
        r = c.post("/v1/workspaces/northwind/chat", headers=h, json=body)
    chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    task = lyst.runtime.get(owner, chunks[0]["messageId"])
    answer = task["result"]["answer"]
    assert task["result"]["supporting"]["narrative_by"].startswith("model"), task["result"]["supporting"]["narrative_by"]
    assert answer.startswith("In short:")
    deltas = [x for x in chunks if x["type"] == "text-delta"]
    assert len(deltas) > 1  # it arrived in pieces, while the task ran
    assert "".join(x["delta"] for x in deltas) == answer and "reset-step" not in [x["type"] for x in chunks]
    if HAVE_AISDK:
        msg = read_with_ai_sdk(r.text)["messages"][-1]
        assert [p["text"] for p in msg["parts"] if p["type"] == "text"] == [answer]
