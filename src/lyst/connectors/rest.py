# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""REST connector: a JSON endpoint that returns a list of records (optionally under a key), with page-based or
cursor-based pagination, a bearer token or API-key header, and an allow-list of hosts.

Declared, not scripted: the manifest fields describe where the records are and how pages link, so a
connection can be set up for most "list" APIs without code. Only https hosts in LYST_REST_ALLOWED_HOSTS
(comma list; empty means any https host) are contacted, and only with GET.

Whatever the allow-list says, a host that resolves to a loopback, link-local (cloud metadata), multicast or
unspecified address is refused unless LYST_REST_ALLOW_PRIVATE=1; private-range (RFC 1918) hosts stay reachable,
because an internal API is an ordinary source, and the allow-list is how an installation narrows that. A next-page
link in a response is followed only on the configured endpoint's own scheme, host and port, so the source cannot
send the token elsewhere.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
import pyarrow as pa

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register

log = logging.getLogger("lyst.connectors.rest")
_policy_logged = False


def _log_policy(allowed: list[str]) -> None:
    global _policy_logged
    if not _policy_logged:
        _policy_logged = True
        log.info("REST connector egress: %s%s%s", f"hosts {', '.join(allowed)}" if allowed else "any https host (LYST_REST_ALLOWED_HOSTS is empty)",
                 "; plain http allowed" if os.environ.get("LYST_REST_ALLOW_HTTP") else "",
                 "; loopback and link-local allowed" if os.environ.get("LYST_REST_ALLOW_PRIVATE") == "1" else "")


def _refuse_internal(host: str, port: int) -> None:
    """Refuse a host that is, or resolves to, the engine's own machine or a link-local (metadata) address."""
    if os.environ.get("LYST_REST_ALLOW_PRIVATE") == "1":
        return
    try:
        addrs = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]
        except (OSError, UnicodeError):
            return  # does not resolve: the request itself fails with a network error
    for ip in addrs:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ConnectorError(f"host {host} is an internal address ({ip}); set LYST_REST_ALLOW_PRIVATE=1 to allow it")


@register
class RestConnector(Connector):
    manifest = Manifest(
        kind="rest", label="REST API (JSON list)", auth="token",
        fields=(Field("url", "Endpoint URL (https)"), Field("records_key", "Key holding the list (empty: the body is the list)", required=False),
                Field("page_param", "Page query parameter (empty: no pagination)", required=False),
                Field("next_key", "Key in the body holding the next page cursor or URL (empty: page numbers)", required=False),
                Field("page_size_param", "Page size parameter", required=False), Field("page_size", "Page size", required=False, default="200"),
                Field("auth_header", "Auth header name (e.g. Authorization or X-API-Key)", required=False, default="Authorization"),
                Field("token", "Token (sent as 'Bearer <token>' in Authorization, raw in other headers)", secret=True, required=False),
                Field("since_param", "Query parameter for incremental pulls (e.g. updated_after)", required=False)),
        notes="GET only; https only; hosts limited by LYST_REST_ALLOWED_HOSTS. Each sync reads every page; with a since "
              "parameter and a cursor column, later syncs ask the API only for newer records.")

    http: httpx.Client | None = None  # tests inject a client

    def _check_url(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme != "https" and not (os.environ.get("LYST_REST_ALLOW_HTTP") and u.scheme == "http"):
            raise ConnectorError("the endpoint must be https")
        if not u.hostname:
            raise ConnectorError("the endpoint URL has no host")
        allowed = [h.strip().lower() for h in os.environ.get("LYST_REST_ALLOWED_HOSTS", "").split(",") if h.strip()]
        _log_policy(allowed)
        if allowed and u.hostname.lower() not in allowed:
            raise ConnectorError(f"host {u.hostname} is not in LYST_REST_ALLOWED_HOSTS")
        _refuse_internal(u.hostname, u.port or (443 if u.scheme == "https" else 80))

    def _same_origin(self, url: str) -> None:
        """A next-page URL from the response body: only the configured endpoint's origin receives the token."""
        a, b = urlparse(self.config["url"]), urlparse(url)
        if (a.scheme, (a.hostname or "").lower(), a.port) != (b.scheme, (b.hostname or "").lower(), b.port):
            raise ConnectorError(f"the next-page link points to another host ({b.hostname}); it is not followed")

    def _headers(self) -> dict[str, str]:
        tok = self.secrets.get("token")
        if not tok:
            return {}
        name = self.config.get("auth_header") or "Authorization"
        return {name: f"Bearer {tok}" if name.lower() == "authorization" else tok}

    def _client(self) -> httpx.Client:
        return self.http or httpx.Client(timeout=30, follow_redirects=False)

    def _pages(self, since: Any = None) -> Iterator[list[dict[str, Any]]]:
        url = self.config["url"]
        self._check_url(url)
        params: dict[str, Any] = {}
        if self.config.get("page_size_param"):
            params[self.config["page_size_param"]] = self.config.get("page_size") or "200"
        if since is not None and self.config.get("since_param"):
            params[self.config["since_param"]] = since
        page, cursor, seen = 1, None, 0
        c = self._client()
        own = self.http is None
        try:
            for _ in range(10_000):  # a hard stop against endless pagination
                q = dict(params)
                if self.config.get("page_param") and not self.config.get("next_key"):
                    q[self.config["page_param"]] = page
                if cursor is not None and self.config.get("next_key"):
                    if str(cursor).startswith("http"):
                        self._same_origin(str(cursor))
                        self._check_url(str(cursor))
                        r = c.get(str(cursor), headers=self._headers())
                    else:
                        q[self.config.get("page_param") or "cursor"] = cursor
                        r = c.get(url, params=q, headers=self._headers())
                else:
                    r = c.get(url, params=q, headers=self._headers())
                if r.status_code != 200:
                    category = {401: "auth", 403: "permission", 404: "not_found", 429: "rate_limit"}.get(
                        r.status_code, "source" if r.status_code >= 500 else None)
                    retry_after = r.headers.get("retry-after")
                    raise ConnectorError(f"{url} returned {r.status_code}", category=category,
                                         retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None)
                body = r.json()
                records = body
                if self.config.get("records_key"):
                    for part in self.config["records_key"].split("."):
                        records = records.get(part, []) if isinstance(records, dict) else []
                if not isinstance(records, list):
                    raise ConnectorError("the endpoint did not return a list of records")
                if not records:
                    return
                yield [rec for rec in records if isinstance(rec, dict)]
                seen += len(records)
                if self.config.get("next_key"):
                    cursor = body
                    for part in self.config["next_key"].split("."):  # "meta.next" reads a nested next link
                        cursor = cursor.get(part) if isinstance(cursor, dict) else None
                    if not cursor:
                        return
                elif self.config.get("page_param"):
                    page += 1
                else:
                    return
        finally:
            if own:
                c.close()

    def test(self) -> dict[str, Any]:
        first = next(self._pages(), [])
        return {"ok": True, "records_in_first_page": len(first), "fields": sorted({k for r in first for k in r})}

    def discover(self) -> list[SourceTable]:
        first = next(self._pages(), [])
        cols = sorted({k for r in first for k in r})
        name = urlparse(self.config["url"]).path.rstrip("/").split("/")[-1] or "records"
        return [SourceTable("", name, [{"name": c, "type": type(next((r[c] for r in first if r.get(c) is not None), "")).__name__} for c in cols])]

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None,
             batch_rows: int = 50_000) -> Iterator[pa.Table]:
        """Pages are gathered into tables of about ``batch_rows`` records; a table's columns are every key seen in
        its records (a record without a key is null there), and the sync unifies the tables' schemas."""
        buf: list[dict[str, Any]] = []
        for page in self._pages(since=after if cursor_column else None):
            if cursor_column and after is not None:
                page = [r for r in page if r.get(cursor_column) is not None and _after(r[cursor_column], after, cursor_column)]
            buf.extend(page)
            if len(buf) >= max(1, batch_rows):
                yield _table(buf)
                buf = []
        if buf:
            yield _table(buf)


def _after(value: Any, after: Any, column: str) -> bool:
    try:
        return value > after
    except TypeError:
        # JSON can change a field's type between syncs (an int timestamp, then an ISO string): compare as text
        # rather than failing the sync half way
        return str(value) > str(after)


def _table(records: list[dict[str, Any]]) -> pa.Table:
    cols = sorted({k for r in records for k in r})
    out = {}
    for c in cols:
        values = [_scalar(r.get(c)) for r in records]
        try:
            out[c] = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):  # a field whose type varies between records is kept as text
            out[c] = pa.array([None if v is None else str(v) for v in values], pa.string())
    return pa.table(out)


def _scalar(v: Any) -> Any:
    if isinstance(v, dict | list):
        import json  # noqa: PLC0415

        return json.dumps(v)
    return v
