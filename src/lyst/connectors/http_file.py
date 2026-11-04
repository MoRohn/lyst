# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""HTTP file connector: one CSV, TSV, Parquet or JSON file at a URL (a published extract, a data portal download,
an object-store link), optionally behind a bearer token or API key.

Each sync downloads the file again (there is no server-side query); with a cursor column, only rows past the
last cursor are appended, so a file that grows (an export with an `updated_at` column) syncs incrementally.
https only, GET only, hosts limited by LYST_REST_ALLOWED_HOSTS, size limited by LYST_HTTP_FILE_MAX_MB (200).
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from . import Connector, ConnectorError, Field, Manifest, SourceTable, register

FORMATS = ("auto", "csv", "tsv", "parquet", "json")


@register
class HttpFileConnector(Connector):
    manifest = Manifest(
        kind="http_file", label="File at a URL (CSV, Parquet, JSON)",
        fields=(Field("url", "File URL (https)"),
                Field("format", "Format: auto, csv, tsv, parquet or json", required=False, default="auto"),
                Field("records_key", "JSON only: key holding the list (empty: the body is the list)", required=False),
                Field("auth_header", "Auth header name (e.g. Authorization or X-API-Key)", required=False, default="Authorization"),
                Field("token", "Token (sent as 'Bearer <token>' in Authorization, raw in other headers)", secret=True, required=False)),
        notes="GET only; https only; hosts limited by LYST_REST_ALLOWED_HOSTS; at most LYST_HTTP_FILE_MAX_MB (200). Each sync "
              "downloads the file; with a cursor column only newer rows are appended.")

    http: httpx.Client | None = None  # tests inject a client

    def _check_url(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme != "https" and not os.environ.get("LYST_REST_ALLOW_HTTP"):
            raise ConnectorError("the URL must be https")
        allowed = [h.strip().lower() for h in os.environ.get("LYST_REST_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed and u.hostname and u.hostname.lower() not in allowed:
            raise ConnectorError(f"host {u.hostname} is not in LYST_REST_ALLOWED_HOSTS")

    def _headers(self) -> dict[str, str]:
        tok = self.secrets.get("token")
        if not tok:
            return {}
        name = self.config.get("auth_header") or "Authorization"
        return {name: f"Bearer {tok}" if name.lower() == "authorization" else tok}

    def _format(self, content_type: str) -> str:
        fmt = (self.config.get("format") or "auto").lower()
        if fmt not in FORMATS:
            raise ConnectorError(f"unknown format {fmt}")
        if fmt != "auto":
            return fmt
        path = urlparse(self.config["url"]).path.lower()
        for ext, name in ((".parquet", "parquet"), (".tsv", "tsv"), (".csv", "csv"), (".json", "json"), (".ndjson", "json"), (".jsonl", "json")):
            if path.endswith(ext):
                return name
        ct = content_type.split(";")[0].strip().lower()
        if "parquet" in ct:
            return "parquet"
        if "json" in ct:
            return "json"
        if "tab-separated" in ct:
            return "tsv"
        return "csv"

    def _fetch(self) -> tuple[bytes, str, dict[str, str]]:
        url = self.config["url"]
        self._check_url(url)
        limit = int(float(os.environ.get("LYST_HTTP_FILE_MAX_MB", "200")) * 1e6)
        c = self.http or httpx.Client(timeout=60, follow_redirects=False)
        try:
            with c.stream("GET", url, headers=self._headers()) as r:
                if r.status_code != 200:
                    raise ConnectorError(f"{url} returned {r.status_code}")
                declared = int(r.headers.get("content-length") or 0)
                if declared > limit:
                    raise ConnectorError(f"the file is {declared / 1e6:.0f} MB; the limit is LYST_HTTP_FILE_MAX_MB={limit / 1e6:.0f}")
                buf = io.BytesIO()
                for chunk in r.iter_bytes():
                    buf.write(chunk)
                    if buf.tell() > limit:
                        raise ConnectorError(f"the file exceeds LYST_HTTP_FILE_MAX_MB={limit / 1e6:.0f}")
                meta = {k: r.headers[k] for k in ("etag", "last-modified", "content-type") if k in r.headers}
                return buf.getvalue(), r.headers.get("content-type", ""), meta
        finally:
            if self.http is None:
                c.close()

    def _table(self) -> tuple[pa.Table, dict[str, str]]:
        data, ctype, meta = self._fetch()
        fmt = self._format(ctype)
        try:
            if fmt == "parquet":
                return pq.read_table(io.BytesIO(data)), meta
            if fmt in ("csv", "tsv"):
                return pacsv.read_csv(io.BytesIO(data), parse_options=pacsv.ParseOptions(delimiter="\t" if fmt == "tsv" else ",")), meta
            text = data.decode("utf-8")
            records: Any
            if "\n" in text.strip() and text.lstrip().startswith("{"):
                records = [json.loads(line) for line in text.splitlines() if line.strip()]  # JSON lines
            else:
                records = json.loads(text)
                for part in (self.config.get("records_key") or "").split("."):
                    if part:
                        records = records.get(part, []) if isinstance(records, dict) else []
            if not isinstance(records, list):
                raise ConnectorError("the JSON body is not a list of records (set the records key)")
            records = [r for r in records if isinstance(r, dict)]
            cols = sorted({k for r in records for k in r})
            return pa.table({c: [_scalar(r.get(c)) for r in records] for c in cols}), meta
        except (pa.ArrowInvalid, ValueError, UnicodeDecodeError) as exc:
            raise ConnectorError(f"could not read the file as {fmt}: {str(exc)[:200]}") from exc

    def _name(self) -> str:
        last = urlparse(self.config["url"]).path.rstrip("/").split("/")[-1] or "file"
        return last.rsplit(".", 1)[0] or "file"

    def test(self) -> dict[str, Any]:
        t, meta = self._table()
        return {"ok": True, "rows": t.num_rows, "columns": t.column_names, **{k.replace("-", "_"): v for k, v in meta.items()}}

    def discover(self) -> list[SourceTable]:
        t, _ = self._table()
        return [SourceTable("", self._name(), [{"name": f.name, "type": str(f.type)} for f in t.schema], t.num_rows)]

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None, batch_rows: int = 50_000) -> Iterator[pa.Table]:
        t, _ = self._table()
        if cursor_column and after is not None:
            if cursor_column not in t.column_names:
                raise ConnectorError(f"no column {cursor_column} in the file")
            col = t[cursor_column]
            try:
                bound = pa.scalar(after).cast(col.type)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                bound = pa.scalar(str(after)) if pa.types.is_string(col.type) else pa.scalar(after)
            t = t.filter(pc.greater(col, bound))
        for batch in t.to_batches(max_chunksize=batch_rows):
            yield pa.Table.from_batches([batch])


def _scalar(v: Any) -> Any:
    if isinstance(v, dict | list):
        return json.dumps(v)
    return v
