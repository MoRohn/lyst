# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Signed webhooks over the event model: subscriptions per workspace, retries with backoff, dead letters, replay.

Each delivery is a POST of one event as JSON with these headers:

* ``Lyst-Event``: the event type; ``Lyst-Delivery``: the delivery id (stable across retries, for idempotency)
* ``Lyst-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>" with the subscription secret>``

Receivers verify the signature and reject a ``t`` older than five minutes (``lyst_client.verify_signature`` does
both). A delivery that does not get a 2xx is retried after 30 s, 1, 2, 4 ... minutes, eight attempts in all, then
marked dead; it can be redelivered by hand. Twenty dead deliveries in a row pause the subscription with the reason.

Targets must be https and must not resolve to a private, loopback or link-local address (no requests into the
engine's own network). ``LYST_WEBHOOK_ALLOW_PRIVATE=1`` lifts both for a closed, on-premises installation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any
from urllib.parse import urlparse

import httpx

from .events import EventService, matches, row_to_event, types
from .security import Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.webhooks")
MAX_ATTEMPTS = 8
PAUSE_AFTER_DEAD = 20
TIMEOUT = 5.0


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _allow_private() -> bool:
    return os.environ.get("LYST_WEBHOOK_ALLOW_PRIVATE", "0") == "1"


DNS_TIMEOUT = 3.0
_resolver = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lyst-webhook-dns")


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address ``host`` resolves to, within DNS_TIMEOUT: a stalled resolver must not wedge a delivery pass."""
    try:
        infos = _resolver.submit(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP).result(timeout=DNS_TIMEOUT)
    except FutureTimeout as exc:
        raise ValueError(f"the webhook host {host} did not resolve within {DNS_TIMEOUT:.0f} s") from exc
    except (socket.gaierror, UnicodeError) as exc:
        raise ValueError(f"the webhook host {host} does not resolve") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def check_target(url: str) -> list[str]:
    """Refuse a URL the engine must not call. Resolved now and again at every delivery (DNS can change).

    Returns the addresses that passed the check, so the caller can connect to one of them rather than resolving
    the name a second time (which a rebinding DNS server could answer differently); empty when the check is lifted.
    """
    from .profiles import offline  # noqa: PLC0415

    p = urlparse(url)
    if p.scheme != "https" and not ((_allow_private() or offline()) and p.scheme == "http"):
        raise ValueError("a webhook URL must be https")
    if not p.hostname:
        raise ValueError("the webhook URL has no host")
    if p.username or p.password:
        raise ValueError("put credentials in the signing secret, not in the webhook URL")
    if not offline() and _allow_private():
        return []
    port = p.port or (443 if p.scheme == "https" else 80)
    ips = _resolve(p.hostname, port)
    for ip in ips:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if offline():
            # an air-gapped installation: only private addresses, whatever else is configured
            if ip.is_global:
                raise ValueError(f"this installation is offline (LYST_PROFILE=offline); {p.hostname} is on the internet")
        elif ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"the webhook host {p.hostname} resolves to a private address ({ip}); set "
                             "LYST_WEBHOOK_ALLOW_PRIVATE=1 on a closed network to allow it")
    return [str(ip) for ip in ips]


def _pinned(url: str, ip: str) -> str:
    """``url`` with its host replaced by the address that passed check_target (the port and path kept)."""
    p = urlparse(url)
    host = f"[{ip}]" if ":" in ip else ip
    return p._replace(netloc=host + (f":{p.port}" if p.port else "")).geturl()


def _retention_days() -> int:
    raw = os.environ.get("LYST_RETENTION_DAYS_EVENTS", "90")
    try:
        days = int(raw)
        if days < 1:
            raise ValueError
    except ValueError:
        log.warning("LYST_RETENTION_DAYS_EVENTS=%r is not a positive whole number of days; using 90", raw)
        return 90
    return days


def backoff(attempts: int) -> int:
    return 30 if attempts <= 1 else min(60 * 2 ** (attempts - 2), 6 * 3600)


class WebhookService:
    def __init__(self, stores: Stores, guard: Guard, events: EventService, *, client: httpx.Client | None = None) -> None:
        self.stores = stores
        self.guard = guard
        self.events = events
        self.client = client  # injectable for tests; created per run otherwise
        self._run_lock = threading.Lock()  # several workers tick; one delivery pass at a time
        self._last_prune = 0.0

    # -- subscriptions ----------------------------------------------------------------------

    def create(self, actor: Principal, ws: str, url: str, *, types_: list[str] | None = None,
               description: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", "webhook")
        check_target(url)
        pats = types_ or ["*"]
        known = types()
        for pat in pats:
            if not any(matches(t, [pat]) for t in known):
                raise ValueError(f"{pat!r} matches no event type; GET /v1/events/types lists them")
        sid = "wh_" + uuid.uuid4().hex[:12]
        secret = "whsec_" + secrets.token_urlsafe(32)
        self.events.collect()
        start = self.stores.control.scalar("SELECT coalesce(max(seq), 0) FROM platform_events") or 0
        self.stores.control.execute(
            "INSERT INTO webhook_subscriptions (id, workspace_id, url, types, secret, description, created_by, after_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [sid, ws, url, json.dumps(pats), secret, description, actor.user_id, start])
        self.guard.audit(actor, ws, "webhook.create", sid, "ok", {"host": urlparse(url).hostname, "types": pats})
        return {**self._get(ws, sid), "secret": secret}

    def _get(self, ws: str, sid: str) -> dict[str, Any]:
        rows = self.stores.control.query(
            "SELECT id, url, types, description, active, disabled_reason, consecutive_failures, created_by, created_at "
            "FROM webhook_subscriptions WHERE id = ? AND workspace_id = ?", [sid, ws])
        if not rows:
            raise KeyError(sid)
        r = rows[0]
        r["types"] = json.loads(r["types"])
        return r

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.admin")
        rows = self.stores.control.query(
            "SELECT id, url, types, description, active, disabled_reason, consecutive_failures, created_by, created_at "
            "FROM webhook_subscriptions WHERE workspace_id = ? ORDER BY created_at", [ws])
        for r in rows:
            r["types"] = json.loads(r["types"])
        return rows

    def update(self, actor: Principal, ws: str, sid: str, *, active: bool | None = None, rotate_secret: bool = False) -> dict[str, Any]:
        self.guard.require(actor, ws, "workspace.admin", sid)
        self._get(ws, sid)
        out: dict[str, Any] = {}
        if active is not None:
            self.stores.control.execute("UPDATE webhook_subscriptions SET active = ?, disabled_reason = NULL, "
                                        "consecutive_failures = 0 WHERE id = ?", [active, sid])
        if rotate_secret:
            out["secret"] = "whsec_" + secrets.token_urlsafe(32)
            self.stores.control.execute("UPDATE webhook_subscriptions SET secret = ? WHERE id = ?", [out["secret"], sid])
        self.guard.audit(actor, ws, "webhook.update", sid, "ok", {"active": active, "rotated": rotate_secret})
        return {**self._get(ws, sid), **out}

    def delete(self, actor: Principal, ws: str, sid: str) -> bool:
        self.guard.require(actor, ws, "workspace.admin", sid)
        self._get(ws, sid)
        self.stores.control.execute("DELETE FROM webhook_deliveries WHERE subscription_id = ?", [sid])
        self.stores.control.execute("DELETE FROM webhook_subscriptions WHERE id = ?", [sid])
        self.guard.audit(actor, ws, "webhook.delete", sid, "ok", {})
        return True

    def deliveries(self, actor: Principal, ws: str, sid: str, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.admin", sid)
        self._get(ws, sid)
        sql = ("SELECT d.id, d.event_seq, e.type, d.status, d.attempts, d.next_attempt_at, d.last_status, d.last_error, "
               "d.created_at, d.delivered_at FROM webhook_deliveries d JOIN platform_events e ON e.seq = d.event_seq "
               "WHERE d.subscription_id = ?")
        params: list[Any] = [sid]
        if status:
            sql += " AND d.status = ?"
            params.append(status)
        return self.stores.control.query(sql + " ORDER BY d.event_seq DESC LIMIT ?", [*params, min(limit, 500)])

    def redeliver(self, actor: Principal, ws: str, delivery_id: str) -> dict[str, Any]:
        rows = self.stores.control.query(
            "SELECT d.id, d.subscription_id, s.active FROM webhook_deliveries d "
            "JOIN webhook_subscriptions s ON s.id = d.subscription_id WHERE d.id = ? AND s.workspace_id = ?", [delivery_id, ws])
        if not rows:
            raise KeyError(delivery_id)
        self.guard.require(actor, ws, "workspace.admin", delivery_id)
        if not rows[0]["active"]:
            # the delivery pass skips paused subscriptions: queuing it would report a send that never happens
            raise ValueError("this webhook is paused; resume it, then redeliver")
        self.stores.control.execute("UPDATE webhook_deliveries SET status = 'pending', attempts = 0, next_attempt_at = now() "
                                    "WHERE id = ?", [delivery_id])
        self.guard.audit(actor, ws, "webhook.redeliver", delivery_id, "ok", {})
        # attempt only this delivery now; the worker tick sends everything else (and this one, if a pass is running)
        self.run(only=delivery_id)
        return self.stores.control.query("SELECT id, status, attempts, last_status, last_error FROM webhook_deliveries "
                                         "WHERE id = ?", [delivery_id])[0]

    def ping(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        """Send a signed ``ping`` now, outside the queue, and report what the receiver answered."""
        self.guard.require(actor, ws, "workspace.admin", sid)
        sub = self.stores.control.query("SELECT url, secret FROM webhook_subscriptions WHERE id = ? AND workspace_id = ?", [sid, ws])
        if not sub:
            raise KeyError(sid)
        event = {"seq": 0, "id": f"ping:{uuid.uuid4().hex[:8]}", "type": "ping", "schema": 1, "workspace_id": ws,
                 "occurred_at": dt.datetime.now(dt.UTC).isoformat(), "actor": actor.user_id,
                 "subject": {"kind": "webhook", "id": sid}, "data": {}, "source": "ping"}
        status, error = self._post(sub[0]["url"], sub[0]["secret"], event, delivery_id=event["id"])
        return {"ok": status is not None and 200 <= status < 300, "status": status, "error": error}

    # -- delivery ---------------------------------------------------------------------------

    def enqueue(self) -> int:
        """A pending delivery for each new event that each active subscription asks for."""
        self.events.collect()
        subs = self.stores.control.query("SELECT id, workspace_id, types, after_seq FROM webhook_subscriptions WHERE active")
        made = 0
        for s in subs:
            pats = json.loads(s["types"])
            rows = self.stores.control.query("SELECT seq, type FROM platform_events WHERE workspace_id = ? AND seq > ? "
                                             "ORDER BY seq LIMIT 1000", [s["workspace_id"], s["after_seq"]])
            if not rows:
                continue
            wanted = [r["seq"] for r in rows if matches(r["type"], pats)]

            def tx(c, s=s, wanted=wanted, last=rows[-1]["seq"]):
                for seq in wanted:
                    c.execute("INSERT OR IGNORE INTO webhook_deliveries (id, subscription_id, event_seq, status) "
                              "VALUES (?, ?, ?, 'pending')", ["whd_" + uuid.uuid4().hex[:14], s["id"], seq])
                c.execute("UPDATE webhook_subscriptions SET after_seq = ? WHERE id = ?", [last, s["id"]])

            self.stores.control.write(tx)
            made += len(wanted)
        return made

    def run(self, *, limit: int = 50, only: str | None = None) -> dict[str, int]:
        """Enqueue new events, then attempt every due delivery once. Called from the worker tick.

        ``only`` attempts that one delivery and nothing else (a hand redelivery), without enqueueing or pruning.
        """
        if not self._run_lock.acquire(blocking=False):
            return {"sent": 0, "failed": 0}
        try:
            if only is None and time.monotonic() - self._last_prune > 86400:
                self._last_prune = time.monotonic()
                try:
                    self._prune()
                except Exception:  # retention must never stop deliveries
                    log.exception("webhook retention prune failed; deliveries continue")
            return self._run(limit, only=only)
        finally:
            self._run_lock.release()

    def _prune(self) -> None:
        self.events.prune(_retention_days())
        self.stores.control.execute("DELETE FROM webhook_deliveries WHERE created_at < now() - INTERVAL 30 DAY "
                                    "AND status IN ('delivered', 'dead')")
        # a delivery whose event has been pruned can never be sent or listed (both join platform_events)
        self.stores.control.execute("DELETE FROM webhook_deliveries d WHERE NOT EXISTS "
                                    "(SELECT 1 FROM platform_events e WHERE e.seq = d.event_seq)")

    def _run(self, limit: int, *, only: str | None = None) -> dict[str, int]:
        if only is None:
            self.enqueue()
        where, params = ("d.id = ?", [only]) if only else ("d.next_attempt_at <= now()", [])
        due = self.stores.control.query(
            "SELECT e.*, d.id AS delivery_id, d.attempts AS delivery_attempts, d.subscription_id, s.url AS target_url, "
            "s.secret AS target_secret FROM webhook_deliveries d "
            "JOIN webhook_subscriptions s ON s.id = d.subscription_id JOIN platform_events e ON e.seq = d.event_seq "
            f"WHERE d.status = 'pending' AND {where} AND s.active ORDER BY d.event_seq LIMIT ?", [*params, limit])
        sent = failed = 0
        client = self.client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)  # one pool for the pass
        try:
            for d in due:
                try:
                    if self._deliver(client, d):
                        sent += 1
                    else:
                        failed += 1
                except Exception as exc:  # one bad row must not stop every delivery queued behind it
                    failed += 1
                    log.exception("webhook delivery %s could not be attempted", d.get("delivery_id"))
                    try:
                        self.stores.control.execute(
                            "UPDATE webhook_deliveries SET status = 'dead', attempts = attempts + 1, last_error = ? "
                            "WHERE id = ?", [f"{type(exc).__name__}: {exc}"[:300], d["delivery_id"]])
                    except Exception:
                        log.exception("could not mark webhook delivery %s dead", d.get("delivery_id"))
        finally:
            if self.client is None:
                client.close()
        return {"sent": sent, "failed": failed}

    def _deliver(self, client: httpx.Client, d: dict[str, Any]) -> bool:
        event = row_to_event(d)
        status, error = self._post(d["target_url"], d["target_secret"], event, delivery_id=d["delivery_id"], client=client)
        attempts = d["delivery_attempts"] + 1
        if status is not None and 200 <= status < 300:
            self.stores.control.execute(
                "UPDATE webhook_deliveries SET status = 'delivered', attempts = ?, last_status = ?, last_error = NULL, "
                "delivered_at = now() WHERE id = ?", [attempts, status, d["delivery_id"]])
            self.stores.control.execute("UPDATE webhook_subscriptions SET consecutive_failures = 0 WHERE id = ?",
                                        [d["subscription_id"]])
            return True
        host = urlparse(d["target_url"]).hostname
        dead = attempts >= MAX_ATTEMPTS or (status is not None and status in (400, 401, 403, 404, 410) and attempts >= 3)
        log.warning("webhook delivery %s to %s failed (subscription %s, attempt %d, status %s)%s", d["delivery_id"], host,
                    d["subscription_id"], attempts, status, "; giving up" if dead else "")
        self.stores.control.execute(
            "UPDATE webhook_deliveries SET status = ?, attempts = ?, last_status = ?, last_error = ?, "
            "next_attempt_at = now() + to_seconds(CAST(? AS BIGINT)) WHERE id = ?",
            ["dead" if dead else "pending", attempts, status, (error or "")[:300] or None, backoff(attempts), d["delivery_id"]])
        if dead:
            self.stores.control.execute("UPDATE webhook_subscriptions SET consecutive_failures = consecutive_failures + 1 "
                                        "WHERE id = ?", [d["subscription_id"]])
            n = self.stores.control.scalar("SELECT consecutive_failures FROM webhook_subscriptions WHERE id = ?",
                                           [d["subscription_id"]])
            if n >= PAUSE_AFTER_DEAD:
                log.error("webhook subscription %s (%s) paused after %d dead deliveries in a row", d["subscription_id"],
                          host, n)
                self.stores.control.execute(
                    "UPDATE webhook_subscriptions SET active = false, disabled_reason = ? WHERE id = ?",
                    [f"paused after {n} deliveries in a row failed every retry; last: {status or error}", d["subscription_id"]])
        return False

    def _post(self, url: str, secret: str, event: dict[str, Any], *, delivery_id: str,
              client: httpx.Client | None = None) -> tuple[int | None, str | None]:
        body = json.dumps(event, separators=(",", ":"), default=str).encode()
        headers = {"content-type": "application/json", "user-agent": "Lyst-Webhooks/1", "lyst-event": event["type"],
                   "lyst-delivery": delivery_id, "lyst-signature": sign(secret, body)}
        try:
            ips = check_target(url)
            target, extensions = url, {}
            if ips:
                # connect to the address that was checked, not to whatever the name resolves to a moment later
                # (DNS rebinding); the Host header and TLS name (SNI and certificate check) stay the real host
                host = urlparse(url).hostname or ""
                target, extensions = _pinned(url, ips[0]), {"sni_hostname": host}
                headers["host"] = urlparse(url).netloc
            own = client is None and self.client is None
            http = client or self.client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
            try:
                resp = http.post(target, content=body, headers=headers, extensions=extensions)
            finally:
                if own:
                    http.close()
            return resp.status_code, None if resp.status_code < 300 else resp.text[:200]
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return None, f"{type(exc).__name__}: {exc}"[:300]


__all__ = ["WebhookService", "check_target", "sign"]
