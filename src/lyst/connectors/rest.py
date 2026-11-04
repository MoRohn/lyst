# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""REST connector: a JSON endpoint that returns a list of records (optionally under a key), with page-based or
cursor-based pagination, a bearer token or API-key header, and an allow-list of hosts.

Declared, not scripted: the manifest fields describe where the records are and how pages link, so a
connection can be set up for most "list" APIs without code. Only https hosts in LYST_REST_ALLOWED_HOSTS
(comma list; empty means any https host) are contacted, and only with GET.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
import pyarrow as pa

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register


@register
class RestConnector(Connector):
    manifest = Manifest(
        kind="rest", label="REST API (JSON list)",
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
        if u.scheme != "https" and not os.environ.get("LYST_REST_ALLOW_HTTP"):
            raise ConnectorError("the endpoint must be https")
        allowed = [h.strip().lower() for h in os.environ.get("LYST_REST_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed and u.hostname and u.hostname.lower() not in allowed:
            raise ConnectorError(f"host {u.hostname} is not in LYST_REST_ALLOWED_HOSTS")

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
                        self._check_url(str(cursor))
                        r = c.get(str(cursor), headers=self._headers())
                    else:
                        q[self.config.get("page_param") or "cursor"] = cursor
                        r = c.get(url, params=q, headers=self._headers())
                else:
                    r = c.get(url, params=q, headers=self._headers())
                if r.status_code != 200:
                    raise ConnectorError(f"{url} returned {r.status_code}")
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
                    cursor = body.get(self.config["next_key"]) if isinstance(body, dict) else None
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
        for page in self._pages(since=after if cursor_column else None):
            if cursor_column and after is not None:
                page = [r for r in page if r.get(cursor_column) is not None and r[cursor_column] > after]
            if not page:
                continue
            cols = sorted({k for r in page for k in r})
            yield pa.table({c: [_scalar(r.get(c)) for r in page] for c in cols})


def _scalar(v: Any) -> Any:
    if isinstance(v, dict | list):
        import json  # noqa: PLC0415

        return json.dumps(v)
    return v
