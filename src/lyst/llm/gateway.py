# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The model gateway: every model call Lyst makes goes through ``Gateway.complete``.

A Python port of Acumane's AcuRout (``docs/ACUROUT.md``, ``packages/agent-core/src/ai-gateway``):

* **Admission.** A call takes a lease before its request starts: total calls in flight under the
  upstream limit, the pool under its limit, background never inside the interactive reserve, the
  tenant (workspace) under its per-pool limit, and estimated tokens in flight under the global and
  per-tenant token limits. Lyst has one engine process, so the count lives in memory, as Acumane's
  does without a database. Waiters queue per pool with a bound and a deadline; past it an
  interactive call is refused as busy and a background call is deferred.
* **Adaptive concurrency.** Each (model, pool) route has a live limit under the pool ceiling: it
  starts at half, grows by one after as many healthy calls as its limit, halves on a 429, 5xx,
  timeout or network failure (at most once every 5 s), and a Retry-After pauses the route (up to
  60 s) while other models keep running.
* **Retries.** Capped exponential backoff with full jitter (250 ms doubling to 4 s) or the
  provider's Retry-After. 429 and 5xx retry up to 3 tries, then the next model in the tier; a
  Retry-After over 10 s moves straight to the next model. 402 is read by
  ``error.metadata.limit_source``: the in-flight budget waits and retries once; a call too big for
  the budget is not retried; exhausted credit is not retried or moved, since every model draws on it.
* **Routing.** Per workload: OpenRouter performance thresholds (not ``sort``) and a default output
  limit, usage accounting on, response healing for non-streamed JSON, Lyst's referer and title.

Differences from Acumane, on purpose: waiters are served first-come rather than round-robin by
tenant (the per-tenant limit still caps any one workspace), and limits are per process.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .policy import DEFAULT_POOL, INTERACTIVE_LATENCY, WORKLOAD_ROUTING, AdmissionLimits, admission_limits

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_SITE = "https://github.com/MoRohn/lyst"
OPENROUTER_TITLE = "Lyst"


class GatewayError(RuntimeError):
    outcome = "error"


class Busy(GatewayError):
    """An interactive call could not get a lease before its deadline."""

    outcome = "busy"

    def __init__(self, retry_after: float, reason: str):
        super().__init__(f"Lyst's model capacity is busy right now ({reason}). Try again in {retry_after:.0f} seconds.")
        self.retry_after = retry_after


class Deferred(GatewayError):
    """A background call could not get a lease; the task step should run on a later tick."""

    outcome = "deferred"


class CreditsExhausted(GatewayError):
    outcome = "credits"


class CallTooLarge(GatewayError):
    outcome = "credits"


class RequestRejected(GatewayError):
    """A 4xx other than 429/402: the request's own fault. Not retried, moves no limit."""


class Exhausted(GatewayError):
    """Every model and retry failed."""


@dataclass
class CallResult:
    text: str
    model: str
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    attempts: int
    queue_ms: int
    latency_ms: int
    raw: dict[str, Any]


@dataclass
class CallRecord:
    """One upstream request, for llm_calls."""

    model: str
    pool: str
    workload: str
    tenant: str
    status: int | None
    outcome: str
    queue_ms: int
    latency_ms: int
    attempt: int
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


# --------------------------------------------------------------------------- adaptive routes


@dataclass
class Route:
    ceiling: int
    limit: int
    inflight: int = 0
    healthy: int = 0
    last_cut: float = 0.0
    paused_until: float = 0.0

    def ok(self, now: float) -> None:
        self.healthy += 1
        if self.healthy >= self.limit and self.limit < self.ceiling:
            self.limit += 1
            self.healthy = 0

    def backoff(self, now: float) -> None:
        self.healthy = 0
        if now - self.last_cut >= 5:
            self.limit = max(1, self.limit // 2)
            self.last_cut = now

    def pause(self, now: float, seconds: float) -> None:
        self.paused_until = max(self.paused_until, now + min(seconds, 60))


# ----------------------------------------------------------------------------- admission


@dataclass
class Lease:
    pool: str
    tenant: str
    tokens: int
    route: tuple[str, str]
    queue_ms: int


@dataclass
class _Counts:
    total: int = 0
    pool: dict[str, int] = field(default_factory=lambda: {"interactive": 0, "background": 0})
    tenant_pool: dict[tuple[str, str], int] = field(default_factory=dict)
    tokens: int = 0
    tenant_tokens: dict[str, int] = field(default_factory=dict)


class Admission:
    def __init__(self, limits: AdmissionLimits, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.counts = _Counts()
        self.routes: dict[tuple[str, str], Route] = {}
        self.waiting = {"interactive": 0, "background": 0}
        self.refusals: dict[str, int] = {}
        self._cv = threading.Condition()

    def route(self, model: str, pool: str) -> Route:
        key = (model, pool)
        if key not in self.routes:
            ceiling = self.limits.pools[pool].max_inflight
            self.routes[key] = Route(ceiling=ceiling, limit=max(1, math.ceil(ceiling / 2)))
        return self.routes[key]

    def _refusal(self, pool: str, tenant: str, tokens: int, route: Route, now: float) -> str | None:
        lim, c = self.limits, self.counts
        p = lim.pools[pool]
        if c.total >= lim.max_inflight:
            return "upstream"
        if pool == "background" and c.total >= lim.max_inflight - lim.interactive_reserve:
            return "reserve"
        if c.pool[pool] >= p.max_inflight:
            return "pool"
        if c.tenant_pool.get((tenant, pool), 0) >= p.per_tenant:
            return "tenant"
        # A call larger than a token limit on its own is let through when nothing else is running.
        if c.tokens + tokens > lim.max_tokens and c.total > 0:
            return "tokens"
        if c.tenant_tokens.get(tenant, 0) + tokens > lim.per_tenant_tokens and c.tenant_tokens.get(tenant, 0) > 0:
            return "tenant_tokens"
        if route.paused_until > now or route.inflight >= route.limit:
            return "route"
        return None

    def acquire(self, pool: str, tenant: str, tokens: int, model: str) -> Lease:
        p = self.limits.pools[pool]
        start = self.clock()
        deadline = start + p.max_wait_s
        with self._cv:
            route = self.route(model, pool)
            reason = self._refusal(pool, tenant, tokens, route, start)
            if reason and self.waiting[pool] >= p.max_queue:
                self.refusals["queue_full"] = self.refusals.get("queue_full", 0) + 1
                raise (Busy(2, "queue full") if pool == "interactive" else Deferred("queue full"))
            self.waiting[pool] += 1
            try:
                while reason:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        self.refusals[reason] = self.refusals.get(reason, 0) + 1
                        if pool == "interactive":
                            raise Busy(max(1.0, route.paused_until - self.clock()), reason)
                        raise Deferred(reason)
                    self._cv.wait(timeout=min(remaining, 0.25))
                    reason = self._refusal(pool, tenant, tokens, route, self.clock())
            finally:
                self.waiting[pool] -= 1
            c = self.counts
            c.total += 1
            c.pool[pool] += 1
            c.tenant_pool[(tenant, pool)] = c.tenant_pool.get((tenant, pool), 0) + 1
            c.tokens += tokens
            c.tenant_tokens[tenant] = c.tenant_tokens.get(tenant, 0) + tokens
            route.inflight += 1
        return Lease(pool, tenant, tokens, (model, pool), int((self.clock() - start) * 1000))

    def release(self, lease: Lease, outcome: str, retry_after: float | None = None) -> None:
        with self._cv:
            c = self.counts
            c.total -= 1
            c.pool[lease.pool] -= 1
            c.tenant_pool[(lease.tenant, lease.pool)] -= 1
            c.tokens -= lease.tokens
            c.tenant_tokens[lease.tenant] -= lease.tokens
            route = self.routes[lease.route]
            route.inflight -= 1
            now = self.clock()
            if outcome == "ok":
                route.ok(now)
            elif outcome in ("throttled", "timeout", "upstream_error", "network"):
                route.backoff(now)
            if retry_after:
                route.pause(now, retry_after)
            self._cv.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            return {"inflight": self.counts.total, "pools": dict(self.counts.pool), "waiting": dict(self.waiting),
                    "tokens": self.counts.tokens, "refusals": dict(self.refusals),
                    "routes": {f"{m}|{p}": {"limit": r.limit, "inflight": r.inflight,
                                            "paused_s": max(0.0, round(r.paused_until - self.clock(), 1))}
                               for (m, p), r in self.routes.items()}}


# ------------------------------------------------------------------------------- the gateway


def retry_delay(attempt: int, retry_after: float | None, rng: random.Random) -> float:
    if retry_after is not None:
        return retry_after
    cap = min(4.0, 0.25 * 2 ** (attempt - 1))
    return rng.uniform(0, cap)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _limit_source(resp: httpx.Response) -> tuple[str | None, str | None]:
    try:
        err = resp.json().get("error", {})
        meta = err.get("metadata") or {}
        return meta.get("limit_source"), meta.get("reason") or err.get("message")
    except (ValueError, AttributeError):
        return None, None


class Gateway:
    MAX_TRIES = 3

    def __init__(self, *, provider: str, base_url: str, api_key: str | None, http: httpx.Client,
                 limits: AdmissionLimits | None = None, sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None, on_call: Callable[[CallRecord], None] | None = None,
                 timeout_s: float = 60.0):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = http
        self.admission = Admission(limits or admission_limits())
        self.sleep = sleep
        self.rng = rng or random.Random()  # noqa: S311 - retry jitter, not security
        self.on_call = on_call or (lambda r: None)
        self.timeout_s = timeout_s
        self.reasoning_refused: set[str] = set()  # models whose provider rejected a reasoning setting the catalog listed

    # -- request shaping --------------------------------------------------------------------

    def _body(self, model: str, messages: list[dict], workload: str, *, json_mode: bool, max_tokens: int | None,
              reasoning_effort: str | None, extra: dict | None, pool: str = "interactive",
              reasoning: dict | None = None) -> dict[str, Any]:
        routing = WORKLOAD_ROUTING.get(workload, {})
        if pool == "interactive" and not routing.get("provider"):
            # a person is waiting even on a short classification: prefer fast endpoints, as chat does
            routing = {**routing, "provider": INTERACTIVE_LATENCY}
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0,
                                "max_tokens": max_tokens or routing.get("max_tokens") or 1024}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
            if routing.get("provider"):
                body["provider"] = routing["provider"]
            if reasoning_effort:
                body["reasoning"] = {"effort": reasoning_effort}
            elif reasoning and model not in self.reasoning_refused:
                body["reasoning"] = reasoning
            if json_mode:
                body["plugins"] = [{"id": "response-healing"}]
        if extra:
            body.update(extra)
        return body

    def _headers(self, tenant: str = "platform") -> dict[str, str]:
        h = {}
        key = self.api_key(tenant) if callable(self.api_key) else self.api_key
        if key:
            h["Authorization"] = f"Bearer {key}"
        if self.provider == "openrouter":
            h["HTTP-Referer"] = OPENROUTER_SITE
            h["X-Title"] = OPENROUTER_TITLE
        return h

    # -- the call ---------------------------------------------------------------------------

    def complete(self, messages: list[dict], *, models: list[str], workload: str = "chat", tenant: str = "platform",
                 pool: str | None = None, json_mode: bool = False, max_tokens: int | None = None,
                 reasoning_effort: str | None = None, extra: dict | None = None,
                 reasoning_for: Callable[[str, str], dict | None] | None = None) -> CallResult:
        """``reasoning_for(model, workload)`` may lower a model's reasoning for a quick call (``policy.quick_reasoning``).
        If the provider rejects that setting the model is remembered and never sent it again; the call moves to the
        next model, or, on the last (or only, pinned) model, retries it once without the setting."""
        pool = pool or DEFAULT_POOL.get(workload, "interactive")
        est = sum(len(m.get("content") or "") for m in messages) // 4 + (
            max_tokens or WORKLOAD_ROUTING.get(workload, {}).get("max_tokens") or 1024)
        tries_total = 0
        last_error = "no models to try"
        for model in models:
            in_flight_budget_retried = False
            length_retried = False
            reasoning = reasoning_for(model, workload) if reasoning_for and not reasoning_effort else None
            attempt = 0
            while attempt < self.MAX_TRIES:
                attempt += 1
                tries_total += 1
                lease = self.admission.acquire(pool, tenant, est, model)
                start = time.monotonic()
                status, outcome, retry_after, resp = None, "error", None, None
                rec = CallRecord(model, pool, workload, tenant, None, "error", lease.queue_ms, 0, tries_total)
                try:
                    resp = self.http.post(f"{self.base_url}/chat/completions", headers=self._headers(tenant),
                                          json=self._body(model, messages, workload, json_mode=json_mode,
                                                          max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                                                          extra=extra, pool=pool, reasoning=reasoning),
                                          timeout=self.timeout_s)
                    status = resp.status_code
                    retry_after = _retry_after(resp)
                    if status == 200:
                        outcome = "ok"
                    elif status == 429:
                        outcome = "throttled"
                    elif status == 402:
                        outcome = "credits"
                    elif status >= 500:
                        outcome = "upstream_error"
                    else:
                        outcome = "rejected"
                except httpx.TimeoutException:
                    outcome = "timeout"
                except httpx.HTTPError:
                    outcome = "network"
                finally:
                    latency = int((time.monotonic() - start) * 1000)
                    self.admission.release(lease, outcome, retry_after if outcome == "throttled" else None)
                rec.status, rec.outcome, rec.latency_ms = status, outcome, latency
                if outcome == "ok":
                    data = resp.json()
                    usage = data.get("usage") or {}
                    rec.provider = data.get("provider")
                    rec.input_tokens = usage.get("prompt_tokens")
                    rec.output_tokens = usage.get("completion_tokens")
                    rec.cost_usd = usage.get("cost")
                    choice = (data.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content = msg.get("content") or ""
                    # A reasoning model can spend the whole budget before the answer, or stop mid-JSON: one retry with a
                    # larger budget on the same model, recorded as "truncated", rather than a cut-off answer.
                    if choice.get("finish_reason") == "length" and not length_retried and (json_mode or not content.strip()):
                        length_retried = True
                        rec.outcome = "truncated"
                        self.on_call(rec)
                        max_tokens = min(4096, (max_tokens or 1024) * 3)
                        attempt -= 1
                        continue
                    if not content.strip() and not msg.get("tool_calls"):
                        # a stop with no text (the model spent its answer thinking, or returned nothing): never a reply;
                        # the next model in the walk is tried, and with none left the call fails like any other
                        rec.outcome = "empty"
                        self.on_call(rec)
                        last_error = f"{model}: empty reply"
                        break
                    self.on_call(rec)
                    return CallResult(content, data.get("model") or model, rec.provider,
                                      rec.input_tokens, rec.output_tokens, rec.cost_usd, tries_total,
                                      lease.queue_ms, latency, data)
                self.on_call(rec)
                if outcome == "rejected" and reasoning and model not in self.reasoning_refused and "reasoning" in resp.text.lower():
                    # the provider refused the lower thinking setting the catalog listed: remember it; walking a tier,
                    # go to the next model (it would think at length here), else retry this one without the setting
                    self.reasoning_refused.add(model)
                    if model != models[-1]:
                        last_error = f"{model}: reasoning setting refused"
                        break
                    attempt -= 1
                    continue
                if outcome == "rejected":
                    raise RequestRejected(f"{model} rejected the request ({status})")
                if outcome == "credits":
                    source, reason = _limit_source(resp)
                    if source == "openrouter_in_flight_budget" and not in_flight_budget_retried:
                        in_flight_budget_retried = True
                        self.sleep(retry_after if retry_after is not None else 1.0)
                        attempt -= 1  # the one budget retry does not count against the 3 tries
                        continue
                    if source == "openrouter_credits" and reason and "weight_exceeds_budget" in reason:
                        raise CallTooLarge("this call alone exceeds the key's budget")
                    raise CreditsExhausted("the OpenRouter key is out of credit or over its limit")
                last_error = f"{model}: {outcome}" + (f" ({status})" if status else "")
                if retry_after is not None and retry_after > 10:
                    break  # long pause asked: go to the next model now
                if attempt < self.MAX_TRIES:
                    self.sleep(retry_delay(attempt, retry_after, self.rng))
        raise Exhausted(f"every model failed; last: {last_error}")

    def embed(self, texts: list[str], *, model: str, tenant: str = "platform") -> tuple[list[list[float]], CallRecord]:
        """Embeddings through the same admission and accounting (background pool, workload embed)."""
        est = sum(len(t) for t in texts) // 4
        lease = self.admission.acquire("background", tenant, est, model)
        start = time.monotonic()
        rec = CallRecord(model, "background", "embed", tenant, None, "error", lease.queue_ms, 0, 1)
        outcome, retry_after = "error", None
        try:
            resp = self.http.post(f"{self.base_url}/embeddings", headers=self._headers(tenant),
                                  json={"model": model, "input": texts, **({"usage": {"include": True}} if self.provider == "openrouter" else {})},
                                  timeout=self.timeout_s)
            rec.status = resp.status_code
            retry_after = _retry_after(resp)
            if resp.status_code == 200:
                outcome = "ok"
                data = resp.json()
                vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]
                usage = data.get("usage") or {}
                rec.input_tokens, rec.cost_usd = usage.get("prompt_tokens") or usage.get("total_tokens"), usage.get("cost")
                return vectors, rec
            outcome = "throttled" if resp.status_code == 429 else ("credits" if resp.status_code == 402 else
                                                                  ("upstream_error" if resp.status_code >= 500 else "rejected"))
            raise RequestRejected(f"embeddings failed ({resp.status_code})") if outcome == "rejected" else \
                Exhausted(f"embeddings failed: {outcome} ({resp.status_code})")
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            raise Exhausted("embeddings timed out") from exc
        except httpx.HTTPError as exc:
            outcome = "network"
            raise Exhausted(f"embeddings network error: {exc}") from exc
        finally:
            rec.outcome, rec.latency_ms = outcome, int((time.monotonic() - start) * 1000)
            self.admission.release(lease, outcome, retry_after if outcome == "throttled" else None)
            self.on_call(rec)

    def key_status(self, tenant: str = "platform") -> dict[str, Any] | None:
        """OpenRouter's view of this key: limit, usage and what remains (GET /api/v1/key)."""
        if self.provider != "openrouter":
            return None
        resp = self.http.get(f"{self.base_url}/key", headers=self._headers(tenant), timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})
