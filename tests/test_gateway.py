# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The model gateway against a stub OpenRouter: routing fields, retries, fallbacks, 402 handling,
admission and adaptive limits (Acumane's AcuRout rules, ported)."""

import json
import threading

import httpx
import pytest

from lyst.llm import policy
from lyst.llm.gateway import (
    Admission,
    Busy,
    CallTooLarge,
    CreditsExhausted,
    Deferred,
    Exhausted,
    Gateway,
    RequestRejected,
)
from lyst.llm.policy import AdmissionLimits, PoolLimits

OK = {"choices": [{"message": {"content": "fine"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                                                "cost": 0.0001}, "provider": "StubCloud"}


def limits(interactive=8, background=3, wait=0.2):
    return AdmissionLimits(max_inflight=12, interactive_reserve=4, max_tokens=1_000_000, per_tenant_tokens=250_000,
                           pools={"interactive": PoolLimits(interactive, 8, 64, wait),
                                  "background": PoolLimits(background, 3, 32, wait)})


def gw(handler, **kw):
    sleeps, calls = [], []
    g = Gateway(provider="openrouter", base_url="https://openrouter.test/api/v1", api_key="sk-or-test",
                http=httpx.Client(transport=httpx.MockTransport(handler)), limits=kw.pop("limits", limits()),
                sleep=sleeps.append, on_call=calls.append, **kw)
    return g, sleeps, calls


def test_request_carries_acumane_routing_fields():
    seen = []

    def handler(req):
        seen.append((dict(req.headers), json.loads(req.content)))
        return httpx.Response(200, json=OK)

    g, _, calls = gw(handler)
    r = g.complete([{"role": "user", "content": "hi"}], models=["a/one"], workload="chat", json_mode=True)
    headers, body = seen[0]
    assert r.text == "fine" and r.provider == "StubCloud" and r.cost_usd == 0.0001
    assert headers["authorization"] == "Bearer sk-or-test"
    assert headers["x-title"] == "Lyst" and "http-referer" in headers
    assert body["usage"] == {"include": True}
    assert body["provider"] == {"preferred_max_latency": {"p50": 2, "p90": 5}}
    assert body["max_tokens"] == 2048 and "sort" not in body["provider"]
    assert body["plugins"] == [{"id": "response-healing"}]
    assert calls[0].pool == "interactive" and calls[0].workload == "chat" and calls[0].outcome == "ok"


def test_background_workload_keeps_default_balancing():
    seen = []
    g, _, calls = gw(lambda req: seen.append(json.loads(req.content)) or httpx.Response(200, json=OK))
    g.complete([{"role": "user", "content": "x"}], models=["a/one"], workload="enrich")
    assert "provider" not in seen[0] and seen[0]["max_tokens"] == 1500 and calls[0].pool == "background"


def test_429_retries_three_times_then_next_model():
    tried = []

    def handler(req):
        model = json.loads(req.content)["model"]
        tried.append(model)
        return httpx.Response(429) if model == "a/one" else httpx.Response(200, json=OK)

    g, sleeps, calls = gw(handler)
    r = g.complete([{"role": "user", "content": "x"}], models=["a/one", "b/two"])
    assert tried == ["a/one", "a/one", "a/one", "b/two"] and r.model == "b/two" and r.attempts == 4
    assert len(sleeps) == 2 and all(0 <= s <= 4 for s in sleeps)
    assert [c.outcome for c in calls] == ["throttled"] * 3 + ["ok"]


def test_long_retry_after_moves_to_next_model_at_once():
    tried = []

    def handler(req):
        model = json.loads(req.content)["model"]
        tried.append(model)
        if model == "a/one":
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json=OK)

    g, sleeps, _ = gw(handler)
    g.complete([{"role": "user", "content": "x"}], models=["a/one", "b/two"])
    assert tried == ["a/one", "b/two"] and sleeps == []
    assert g.admission.routes[("a/one", "interactive")].paused_until > 0


def test_short_retry_after_is_honoured():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(503, headers={"Retry-After": "2"}) if n["i"] == 1 else httpx.Response(200, json=OK)

    g, sleeps, _ = gw(handler)
    g.complete([{"role": "user", "content": "x"}], models=["a/one"])
    assert sleeps == [2.0]


def test_credits_exhausted_is_not_retried_or_moved():
    tried = []

    def handler(req):
        tried.append(json.loads(req.content)["model"])
        return httpx.Response(402, json={"error": {"message": "Insufficient credits",
                                                   "metadata": {"limit_source": "openrouter_credits"}}})

    g, _, _ = gw(handler)
    with pytest.raises(CreditsExhausted):
        g.complete([{"role": "user", "content": "x"}], models=["a/one", "b/two"])
    assert tried == ["a/one"]


def test_call_too_large_for_budget():
    g, _, _ = gw(lambda req: httpx.Response(402, json={"error": {"metadata": {
        "limit_source": "openrouter_credits", "reason": "weight_exceeds_budget"}}}))
    with pytest.raises(CallTooLarge):
        g.complete([{"role": "user", "content": "x"}], models=["a/one"])


def test_in_flight_budget_waits_and_retries_once():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(402, headers={"Retry-After": "1"},
                                  json={"error": {"metadata": {"limit_source": "openrouter_in_flight_budget"}}})
        return httpx.Response(200, json=OK)

    g, sleeps, _ = gw(handler)
    assert g.complete([{"role": "user", "content": "x"}], models=["a/one"]).text == "fine"
    assert sleeps == [1.0]


def test_other_4xx_is_not_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(400, json={"error": {"message": "bad"}})

    g, _, _ = gw(handler)
    with pytest.raises(RequestRejected):
        g.complete([{"role": "user", "content": "x"}], models=["a/one", "b/two"])
    assert n["i"] == 1


def test_every_model_failing_is_exhausted():
    g, _, calls = gw(lambda req: httpx.Response(500))
    with pytest.raises(Exhausted):
        g.complete([{"role": "user", "content": "x"}], models=["a/one", "b/two"])
    assert len(calls) == 6


def test_timeouts_are_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json=OK)

    g, _, calls = gw(handler)
    g.complete([{"role": "user", "content": "x"}], models=["a/one"])
    assert [c.outcome for c in calls] == ["timeout", "ok"]


def test_interactive_busy_and_background_deferred_when_pool_is_full():
    a = Admission(limits(interactive=1, background=1, wait=0.1))
    held = a.acquire("interactive", "ws1", 100, "m")
    with pytest.raises(Busy):
        a.acquire("interactive", "ws2", 100, "m")
    bg = a.acquire("background", "ws1", 100, "m")
    with pytest.raises(Deferred):
        a.acquire("background", "ws2", 100, "m")
    a.release(held, "ok")
    a.release(bg, "ok")
    assert a.snapshot()["inflight"] == 0 and a.snapshot()["refusals"]["pool"] == 2


def test_background_never_takes_the_interactive_reserve():
    lim = AdmissionLimits(max_inflight=4, interactive_reserve=2, pools={
        "interactive": PoolLimits(4, 8, 64, 0.05), "background": PoolLimits(4, 8, 32, 0.05)})
    a = Admission(lim)
    a.acquire("background", "t", 1, "m")
    a.acquire("background", "t", 1, "m2")
    with pytest.raises(Deferred):
        a.acquire("background", "t", 1, "m3")
    a.acquire("interactive", "t", 1, "m4")  # the reserve is still there for people


def test_per_tenant_limit_protects_other_workspaces():
    lim = AdmissionLimits(max_inflight=10, interactive_reserve=0, pools={
        "interactive": PoolLimits(10, 2, 64, 0.05), "background": PoolLimits(3, 3, 32, 0.05)})
    a = Admission(lim)
    a.acquire("interactive", "big", 1, "m")
    a.acquire("interactive", "big", 1, "m2")
    with pytest.raises(Busy):
        a.acquire("interactive", "big", 1, "m3")
    a.acquire("interactive", "small", 1, "m3")


def test_waiter_gets_the_lease_when_one_is_released():
    a = Admission(limits(interactive=1, wait=2))
    held = a.acquire("interactive", "ws", 1, "m")
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("lease", a.acquire("interactive", "ws2", 1, "m")))
    t.start()
    a.release(held, "ok")
    t.join(3)
    assert "lease" in got


def test_adaptive_limit_halves_on_throttle_and_grows_when_healthy():
    clock = {"t": 100.0}
    a = Admission(limits(interactive=8), clock=lambda: clock["t"])
    r = a.route("m", "interactive")
    assert r.limit == 4
    lease = a.acquire("interactive", "t", 1, "m")
    a.release(lease, "throttled")
    assert r.limit == 2
    lease = a.acquire("interactive", "t", 1, "m")
    a.release(lease, "throttled")  # within 5 s: not cut again
    assert r.limit == 2
    for _ in range(2):
        lease = a.acquire("interactive", "t", 1, "m")
        a.release(lease, "ok")
    assert r.limit == 3


def test_tiers_follow_acumane_catalog():
    assert policy.tier_of("max") == "frontier" and policy.tier_of("standard") == "balanced"
    assert policy.attempts("balanced")[0] == policy.tier_models("balanced")[0]["modelId"]
    assert policy.attempts("balanced", "openai/gpt-5.4") == ["openai/gpt-5.4"]
    assert len(policy.attempts("economy")) >= 2
