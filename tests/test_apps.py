# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst Apps (``lyst.apps``): a closed catalog of blocks bound to governed metrics, checked and resolved block by block
as a model writes the spec, kept as versioned assets, and streamed to any AI SDK client."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst import uistream
from lyst.api import create_app
from lyst.apps import AppInvalid, Checker, SpecReader
from lyst.security import AccessDenied
from lyst.semantic import MetricSpec, compile_metric

from .test_live_drafts import SSE, _gw, _sse
from .test_uistream import needs_aisdk, read_with_ai_sdk

WS = "northwind"


def _metrics(lyst, owner):
    return {m.name: m for m in lyst.semantic.metrics(owner, WS)}


# -- checking ---------------------------------------------------------------------------------------------------------

def test_blocks_bind_only_to_approved_metrics_and_text_holds_no_figures(seeded):
    lyst, owner = seeded
    ck = Checker(_metrics(lyst, owner))
    assert ck.title("Revenue by region, 2026 and Q3", "FY26 view") == ("Revenue by region, 2026 and Q3", "FY26 view")
    for bad in ("Revenue up 12%", "Revenue hit $2.7M", "Top 5 regions"):
        with pytest.raises(AppInvalid, match="figures"):
            ck.title(bad)
    assert ck.block({"id": "rev", "type": "kpi", "metric": "revenue"}) == {"id": "rev", "type": "kpi", "metric": "revenue",
                                                                          "grain": "month"}
    rules = [({"id": "x", "type": "kpi", "metric": "profit"}, "not an approved metric"),
             ({"id": "y", "type": "breakdown", "metric": "tickets", "dimension": "region"}, "cannot be split by region"),
             ({"id": "rev", "type": "trend", "metric": "revenue"}, "used twice"),
             ({"id": "n", "type": "note", "text": "Margin fell 3 points"}, "figures"),
             ({"id": "s", "type": "script", "code": "fetch()"}, "type"),
             ({"id": "Bad Id", "type": "note", "text": "x"}, "block id"),
             ({"id": "w", "type": "whatif", "metric": "revenue", "min_pct": 0, "max_pct": 0}, "range")]
    for raw, why in rules:
        with pytest.raises(AppInvalid, match=why):
            ck.block(raw)
    assert ck.block({"id": "f", "type": "filter", "dimension": "region"})["dimension"] == "region"
    with pytest.raises(AppInvalid, match="at most one filter"):
        ck.block({"id": "g", "type": "filter", "dimension": "product_line"})
    t = ck.block({"id": "t", "type": "trend", "metric": "revenue", "periods": 999, "value": 123})
    assert t["periods"] == 60 and "value" not in t  # a value in a block is never read


def test_the_reader_finds_each_block_as_soon_as_it_closes():
    text = json.dumps({"title": "Margin {watch}", "subtitle": "by \"region\"", "blocks": [
        {"id": "a", "type": "note", "text": "braces } and ] in text"}, {"id": "b", "type": "kpi", "metric": "revenue"}]})
    r, events = SpecReader(), []
    for ch in text:
        events += [(len(r.text), e) for e in r.feed(ch)]
    assert [e[1][0] for e in events] == ["title", "block", "block"]
    assert events[0][1][1:] == ("Margin {watch}", 'by "region"')
    assert events[1][1][1]["text"] == "braces } and ] in text"
    assert events[1][0] == text.index('}, {"id": "b"') + 1  # the block was read the moment its object closed
    r.reset()
    assert r.feed('{"blocks": [{"id": "a", "type": "note", "text": "x"}], "title": "Late"}')[0] == ("title", None, None)
    assert r.tail() == ("Late", None)


# -- values -----------------------------------------------------------------------------------------------------------

def test_every_value_comes_from_the_semantic_layer(seeded):
    lyst, owner = seeded
    spec = {"title": "Revenue", "blocks": [
        {"id": "f", "type": "filter", "dimension": "region"},
        {"id": "rev", "type": "kpi", "metric": "revenue"},
        {"id": "trend", "type": "trend", "metric": "operating_margin", "periods": 6},
        {"id": "split", "type": "breakdown", "metric": "revenue", "dimension": "region"},
        {"id": "tix", "type": "kpi", "metric": "tickets"},
        {"id": "note", "type": "note", "text": "Figures are governed."}]}
    res = lyst.apps.resolve(owner, WS, spec)
    m = _metrics(lyst, owner)
    series = lyst.data.query(owner, WS, compile_metric(m["revenue"], grain="month")).rows
    kpi = res["blocks"]["rev"]
    assert kpi["latest"]["value"] == pytest.approx(float(series[-1][1]))
    assert kpi["latest"]["prior"] == pytest.approx(float(series[-2][1]))
    assert kpi["metric"]["format"] == "currency" and kpi["metric"]["version"] == 1
    # the filter narrows with values the SQL computed per region: a ratio is never re-aggregated by the client
    by = lyst.data.query(owner, WS, compile_metric(m["operating_margin"], grain="month", by=["region"])).rows
    regions = sorted({r[1] for r in by})
    assert res["blocks"]["f"]["values"] == regions
    east = [r for r in by if r[1] == regions[0]][-6:]
    assert [p["value"] for p in res["blocks"]["trend"]["by"]["values"][regions[0]]] == pytest.approx([float(r[2]) for r in east])
    assert "by" not in res["blocks"]["tix"]  # tickets has no region: the filter leaves it as it is
    split = res["blocks"]["split"]
    assert len(split["periods"]) == 1 and {r["key"] for r in split["rows"]} == set(regions)
    assert res["sources"]["datasets"] == {"pnl": 1, "demand": 1} and set(res["sources"]["metrics"]) == {
        "revenue", "operating_margin", "tickets"}


# -- generating -------------------------------------------------------------------------------------------------------

def test_without_a_model_the_rules_build_the_app_and_it_is_saved_as_an_asset(seeded):
    lyst, owner = seeded
    events: list = []
    out = lyst.apps.generate(owner, WS, "An app to watch operating margin and revenue by region", emit=events.append,
                             prompt_id="p1")
    assert out["source"] == "rules" and out["dropped"] == []
    kinds = [e["type"] for e in events]
    assert kinds[0] == "title" and set(kinds[1:]) == {"block", "update"}
    # KPIs drawn before the filter arrived are sent again with the values it narrows them to
    updated = [e for e in events if e["type"] == "update"]
    assert updated and all(e["data"]["by"]["dimension"] == "region" for e in updated)
    assert kinds.index("update") > kinds.index("block", kinds.index("block") + 1)
    blocks = out["spec"]["blocks"]
    assert {b["type"] for b in blocks} >= {"kpi", "filter", "trend", "breakdown", "whatif"}
    assert next(b for b in blocks if b["type"] == "filter")["dimension"] == "region"
    app = lyst.apps.get(owner, WS, out["app"]["app_id"])
    assert app["version"] == 1 and app["spec"] == out["spec"] and app["depends"]["metrics"]
    asset = lyst.assets.get(owner, WS, out["app"]["asset"]["asset_id"])
    assert asset["kind"] == "app" and asset["health"] == "current" and asset["validation"] == "verified"
    # a retried request saves no second app
    again = lyst.apps.generate(owner, WS, "An app to watch operating margin and revenue by region", emit=lambda e: None,
                               prompt_id="p1")
    assert again["app"]["app_id"] == out["app"]["app_id"] and len(lyst.apps.list(owner, WS)) == 1
    # a viewer can generate one to look at, but only editors save
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, WS, viewer.user_id, "viewer")
    assert lyst.apps.generate(viewer, WS, "revenue", emit=lambda e: None)["app"] is None
    with pytest.raises(AccessDenied, match="data.ingest"):
        lyst.apps.save(viewer, WS, out["spec"])


class _Model:
    """Stands in for the gateway: streams a reply in pieces, as ``Gateway.complete`` passes them on."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def available_for(self, ws):
        return True

    def chat(self, ws, messages, *, on_delta=None, stream_json=False, json_mode=False, **kw):
        self.calls.append({"messages": messages, "stream_json": stream_json, "json_mode": json_mode, **kw})
        reply = ""
        for reply in self.replies:
            if reply is None:
                on_delta(None)
                continue
            for i in range(0, len(reply), 7):
                on_delta(reply[i:i + 7])
        return reply


def _reply(*blocks, title="Margin watch"):
    return json.dumps({"title": title, "blocks": list(blocks)})


def test_a_model_writes_the_spec_and_each_block_is_checked_as_it_arrives(seeded):
    lyst, owner = seeded
    good = [{"id": "margin", "type": "kpi", "metric": "operating_margin"},
            {"id": "why", "type": "note", "text": "Margin rose 4 points last quarter"},  # a figure: left out
            {"id": "profit", "type": "kpi", "metric": "profit"},  # not a metric: left out
            {"id": "trend", "type": "trend", "metric": "operating_margin", "grain": "quarter", "periods": 8}]
    model = _Model(_reply({"id": "first", "type": "kpi", "metric": "revenue"}), None, _reply(*good))
    lyst.apps.llm = model
    events: list = []
    out = lyst.apps.generate(owner, WS, "watch our margin", emit=events.append)
    assert model.calls[0]["stream_json"] and model.calls[0]["json_mode"] and model.calls[0]["workload"] == "structured"
    assert "operating_margin" in model.calls[0]["messages"][0]["content"]  # the inventory it may bind to
    assert out["source"] == "model" and [b["id"] for b in out["spec"]["blocks"]] == ["margin", "trend"]
    assert [d["block"] for d in out["dropped"]] == ["why", "profit"]
    seq = [e["type"] for e in events]
    assert seq == ["title", "block", "reset", "title", "block", "dropped", "dropped", "block"]
    assert out["blocks"]["trend"]["grain"] == "quarter" and len(out["blocks"]["trend"]["series"]) <= 8


def test_a_spec_with_no_usable_block_falls_back_to_the_rules(seeded):
    lyst, owner = seeded
    lyst.apps.llm = _Model(_reply({"id": "x", "type": "kpi", "metric": "nope"}, title="Up 40%"))
    events: list = []
    out = lyst.apps.generate(owner, WS, "revenue by region", emit=events.append)
    assert out["source"] == "rules" and out["spec"]["blocks"][0]["metric"] == "revenue"
    assert {d["block"] for d in out["dropped"]} == {"title", "x"}  # the model's attempt is still reported
    assert "reset" not in [e["type"] for e in events]  # nothing of the model's had been shown


def test_the_gateway_streams_json_only_when_the_caller_checks_it():
    g, _ = _gw(lambda req: httpx.Response(200, content=_sse('{"title": ', '"A"}'), headers=SSE))
    got: list = []
    r = g.complete([{"role": "user", "content": "x"}], models=["a/one"], json_mode=True, on_delta=got.append, stream_json=True)
    assert got == ['{"title": ', '"A"}'] and r.text == '{"title": "A"}'


# -- assets ----------------------------------------------------------------------------------------------------------

def test_a_redefined_metric_invalidates_the_app_and_a_repair_saves_it_again(seeded):
    lyst, owner = seeded
    saved = lyst.apps.save(owner, WS, {"title": "Revenue", "blocks": [{"id": "rev", "type": "kpi", "metric": "revenue"}]})
    aid = saved["asset"]["asset_id"]
    spec = lyst.semantic.get(owner, WS, "revenue")
    lyst.semantic.define(owner, WS, MetricSpec(**{**spec.model_dump(), "description": "Recognised revenue."}), note="wording")
    lyst.assets.scan(WS, owner)
    a = lyst.assets.get(owner, WS, aid)
    assert a["health"] == "invalidated" and a["proposal"]["action"] == "resave"
    out = lyst.assets.repair(owner, WS, aid)
    assert out["app_version"] == 2 and lyst.assets.get(owner, WS, aid)["health"] == "current"
    assert lyst.apps.get(owner, WS, saved["app_id"])["depends"]["metrics"]["revenue"] == 2
    assert lyst.apps.get(owner, WS, saved["app_id"], version=1)["depends"]["metrics"]["revenue"] == 1
    lyst.assets.retire(owner, WS, aid)
    assert lyst.apps.get(owner, WS, saved["app_id"])["status"] == "retired"
    with pytest.raises(AppInvalid, match="retired"):
        lyst.apps.open(owner, WS, saved["app_id"])


# -- the stream ------------------------------------------------------------------------------------------------------

def _generate(seeded, text="Build me an app for revenue by region"):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    body = {"id": "c1", "messageId": "m1",
            "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": text}]}]}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post(f"/v1/workspaces/{WS}/apps/generate", headers=h, json=body)
        listed = c.get(f"/v1/workspaces/{WS}/apps", headers=h).json()
        opened = c.get(f"/v1/workspaces/{WS}/apps/{listed[0]['id']}/open", headers=h).json() if listed else None
        cat = c.get(f"/v1/workspaces/{WS}/apps/catalog", headers=h).json()
    return r, listed, opened, cat


def test_the_generate_route_streams_the_app(seeded):
    r, listed, opened, cat = _generate(seeded)
    assert r.status_code == 200 and r.headers["x-vercel-ai-ui-message-stream"] == "v1"
    chunks = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: {")]
    types = [c["type"] for c in chunks]
    assert types[:3] == ["start", "start-step", "tool-input-start"] and types[-2:] == ["finish-step", "finish"]
    assert r.text.endswith(uistream.DONE)
    # the input text is a checked prefix, and once whole it is exactly the spec
    text = "".join(c["inputTextDelta"] for c in chunks if c["type"] == "tool-input-delta")
    final = next(c for c in chunks if c["type"] == "tool-input-available")
    assert json.loads(text) == final["input"]
    out = next(c for c in chunks if c["type"] == "tool-output-available")["output"]
    assert out["saved"] and out["appId"] == listed[0]["id"]
    sent = [c["data"]["block"]["id"] for c in chunks if c["type"] == "data-lyst-app-block"]
    assert list(dict.fromkeys(sent)) == [b["id"] for b in final["input"]["blocks"]]  # updates reuse a block's id
    assert {c["sourceId"] for c in chunks if c["type"] == "source-document"} == {"pnl@1"}
    assert opened["spec"] == final["input"] and set(opened["blocks"]) == {b["id"] for b in final["input"]["blocks"]}
    assert set(cat["blocks"]) == {"kpi", "trend", "breakdown", "table", "filter", "whatif", "note"}


@needs_aisdk
def test_the_ai_sdk_reads_the_app_as_it_grows(seeded):
    r, *_ = _generate(seeded)
    got = read_with_ai_sdk(r.text)
    assert got["error"] is None, got["error"]
    tool = lambda m: next(p for p in m["parts"] if p["type"] == "dynamic-tool")  # noqa: E731
    snaps = [m for m in got["messages"] if any(p["type"] == "dynamic-tool" for p in m["parts"])]
    growing = [len((tool(m).get("input") or {}).get("blocks") or []) for m in snaps if tool(m)["state"] == "input-streaming"]
    assert growing and growing == sorted(growing) and max(growing) >= 3  # useChat sees the spec grow, block by block
    last = got["messages"][-1]
    t = tool(last)
    assert t["toolName"] == "lyst_app" and t["state"] == "output-available" and t["output"]["saved"]
    data = [p for p in last["parts"] if p["type"] == "data-lyst-app-block"]
    assert [p["data"]["block"]["id"] for p in data] == [b["id"] for b in t["input"]["blocks"]]
    assert any(p["type"] == "source-document" for p in last["parts"])


@needs_aisdk
def test_the_ai_sdk_reads_a_retry_and_a_failure():
    s = uistream.AppStream("ag_1")
    chunks = s.start() + s.event({"type": "title", "title": "A", "subtitle": None}) + s.event(
        {"type": "block", "block": {"id": "a", "type": "note", "text": "x"}, "data": {}}) + s.event({"type": "reset"})
    chunks += s.event({"type": "dropped", "block": "b", "reason": "not a metric"}) + s.fail("no block could be built")
    got = read_with_ai_sdk("".join(uistream.sse(c) for c in chunks) + uistream.DONE)
    last = got["messages"][-1]
    tools = [p for p in last["parts"] if p["type"] == "dynamic-tool"]
    assert len(tools) == 1 and tools[0]["toolCallId"] == "ag_1:app:2" and tools[0]["state"] == "output-error"
    assert not any(p["type"] == "data-lyst-app-block" for p in last["parts"])  # the first attempt was removed
    assert any(p["type"] == "data-lyst-app-dropped" for p in last["parts"])
