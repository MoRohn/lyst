# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The client. Thin on purpose: every method is one documented route; the engine does the governing."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

API_VERSION = "1"
RETRY_STATUS = {429, 502, 503, 504}
TERMINAL = {"done", "failed", "cancelled", "skipped"}


class LystError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class AuthError(LystError):
    pass


class NotFound(LystError):
    pass


class Invalid(LystError):
    pass


class RateLimited(LystError):
    def __init__(self, status: int, message: str, body: Any = None, retry_after: float | None = None) -> None:
        super().__init__(status, message, body)
        self.retry_after = retry_after


class ServerError(LystError):
    pass


class TaskFailed(LystError):
    def __init__(self, task: dict[str, Any]) -> None:
        super().__init__(200, f"task {task.get('id')} ended {task.get('status')}: {task.get('error') or ''}".strip(), task)
        self.task = task


def _error(resp: httpx.Response) -> LystError:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    detail = body.get("detail", body) if isinstance(body, dict) else body
    message = detail.get("detail") or detail.get("error") if isinstance(detail, dict) else str(detail)
    message = str(message)[:500]
    if resp.status_code in (401, 403):
        return AuthError(resp.status_code, message, body)
    if resp.status_code == 404:
        return NotFound(resp.status_code, message, body)
    if resp.status_code in (400, 409, 413, 422):
        return Invalid(resp.status_code, message, body)
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        return RateLimited(429, message, body, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
    return ServerError(resp.status_code, message, body)


def verify_signature(secret: str, header: str, body: bytes, *, tolerance: int = 300, now: float | None = None) -> bool:
    """True when ``Lyst-Signature`` matches ``body`` and is no older than ``tolerance`` seconds."""
    try:
        parts = dict(kv.split("=", 1) for kv in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((time.time() if now is None else now) - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    executed_sql: str
    ms: int
    datasets: list[str]

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=True)) for r in self.rows]

    def to_pandas(self):
        import pandas as pd  # noqa: PLC0415

        return pd.DataFrame(self.rows, columns=self.columns)


class Lyst:
    """A connection to one Lyst engine. ``http`` may be any httpx.Client (a test client, a proxy-configured one)."""

    def __init__(self, url: str | None = None, token: str | None = None, *, http: httpx.Client | None = None,
                 timeout: float = 60.0, retries: int = 4) -> None:
        self.url = (url or os.environ.get("LYST_URL") or "http://127.0.0.1:8770").rstrip("/")
        self.token = token or os.environ.get("LYST_TOKEN")
        self.http = http or httpx.Client(base_url=self.url, timeout=timeout)
        self.retries = retries
        self._warned = False

    # -- transport --------------------------------------------------------------------------

    def request(self, method: str, path: str, *, idempotent: bool | None = None, **kw) -> Any:
        headers = dict(kw.pop("headers", None) or {})
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        headers.setdefault("user-agent", "lyst-client/0.1")
        retry = method in ("GET", "HEAD", "DELETE") if idempotent is None else idempotent
        attempt = 0
        while True:
            try:
                resp = self.http.request(method, path, headers=headers, **kw)
            except httpx.TransportError:
                if not retry or attempt >= self.retries:
                    raise
                attempt += 1
                time.sleep(min(0.5 * 2 ** attempt, 8))
                continue
            self._check_version(resp)
            if resp.status_code in RETRY_STATUS and retry and attempt < self.retries:
                attempt += 1
                ra = resp.headers.get("retry-after")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 0.5 * 2 ** attempt
                time.sleep(min(wait, 30))
                continue
            if resp.status_code >= 400:
                raise _error(resp)
            if resp.status_code == 204 or not resp.content:
                return None
            ctype = resp.headers.get("content-type", "")
            return resp.json() if "json" in ctype else resp.content

    def _check_version(self, resp: httpx.Response) -> None:
        v = resp.headers.get("lyst-api-version")
        if v and v != API_VERSION and not self._warned:
            self._warned = True
            warnings.warn(f"the engine speaks Lyst API version {v}; this client knows {API_VERSION}. Upgrade lyst-client.",
                          stacklevel=3)

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    # -- top level --------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self.get("/health")

    def me(self) -> dict[str, Any]:
        return self.get("/v1/me")

    def workspaces(self) -> list[dict[str, Any]]:
        return self.me().get("workspaces", [])

    def workspace(self, ws: str) -> Workspace:
        return Workspace(self, ws)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.get(f"/v1/tasks/{task_id}")

    def wait(self, task_id: str, *, timeout: float = 600, poll: float = 1.0) -> dict[str, Any]:
        """Poll until the task ends or waits for an approval. Raises TaskFailed for a failed task."""
        deadline = time.monotonic() + timeout
        while True:
            t = self.task(task_id)
            if t["status"] in TERMINAL or t["status"] == "awaiting_approval":
                if t["status"] == "failed":
                    raise TaskFailed(t)
                return t
            if time.monotonic() > deadline:
                raise TimeoutError(f"task {task_id} still {t['status']} after {timeout:.0f}s")
            time.sleep(poll)
            poll = min(poll * 1.5, 5)

    def event_types(self) -> dict[str, Any]:
        return self.get("/v1/events/types")


class _Files:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def _payload(self, file: str | Path | bytes, filename: str | None) -> tuple[str, bytes]:
        if isinstance(file, bytes):
            return filename or "upload", file
        p = Path(file)
        return filename or p.name, p.read_bytes()

    def formats(self) -> dict[str, Any]:
        return self.ws.lyst.get("/v1/ingest/formats")

    def inspect(self, file: str | Path | bytes, *, filename: str | None = None, classification: str = "internal") -> dict[str, Any]:
        name, data = self._payload(file, filename)
        return self.ws.lyst.request("POST", f"{self.ws.base}/ingest", files={"file": (name, data)},
                                    data={"classification": classification})

    def load(self, file: str | Path | bytes, *, filename: str | None = None, classification: str = "internal") -> dict[str, Any]:
        """Inspect and load with the plan's own choices in one call."""
        name, data = self._payload(file, filename)
        return self.ws.lyst.request("POST", f"{self.ws.base}/ingest", files={"file": (name, data)},
                                    data={"classification": classification, "commit": "true"})

    def commit(self, job_id: str, *, tables: list[dict[str, Any]] | None = None, document: bool | None = None) -> dict[str, Any]:
        return self.ws.lyst.request("POST", f"{self.ws.base}/ingest/{job_id}/commit",
                                    json={"tables": tables or [], "document": document})

    def discard(self, job_id: str) -> dict[str, Any]:
        return self.ws.lyst.request("DELETE", f"{self.ws.base}/ingest/{job_id}")

    def list(self) -> list[dict[str, Any]]:
        return self.ws.lyst.get(f"{self.ws.base}/ingest")


class _Events:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def list(self, *, after: int = 0, types: list[str] | None = None, limit: int = 100, wait: float = 0) -> dict[str, Any]:
        return self.ws.lyst.get(f"{self.ws.base}/events", after=after, types=",".join(types) if types else None,
                                limit=limit, wait=wait or None)

    def stream(self, *, after: int | None = None, types: list[str] | None = None, wait: float = 25,
               stop_after: float | None = None) -> Iterator[dict[str, Any]]:
        """Yield events as they happen. Starts at the current end unless ``after`` is given; ``stop_after`` seconds
        ends the stream (for scripts and tests)."""
        cursor = self.list(after=-1)["next"] if after is None else after  # -1: "from now"
        end = time.monotonic() + stop_after if stop_after else None
        while end is None or time.monotonic() < end:
            remaining = wait if end is None else max(0.0, min(wait, end - time.monotonic()))
            page = self.list(after=cursor, types=types, wait=remaining)
            yield from page["events"]
            cursor = page["next"]


class _Webhooks:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def list(self) -> list[dict[str, Any]]:
        return self.ws.lyst.get(f"{self.ws.base}/webhooks")

    def create(self, url: str, *, types: list[str] | None = None, description: str | None = None) -> dict[str, Any]:
        return self.ws.lyst.request("POST", f"{self.ws.base}/webhooks",
                                    json={"url": url, "types": types or ["*"], "description": description})

    def delete(self, sid: str) -> dict[str, Any]:
        return self.ws.lyst.request("DELETE", f"{self.ws.base}/webhooks/{sid}")

    def ping(self, sid: str) -> dict[str, Any]:
        return self.ws.lyst.request("POST", f"{self.ws.base}/webhooks/{sid}/ping")

    def deliveries(self, sid: str, *, status: str | None = None) -> list[dict[str, Any]]:
        return self.ws.lyst.get(f"{self.ws.base}/webhooks/{sid}/deliveries", status=status)

    def redeliver(self, delivery_id: str) -> dict[str, Any]:
        return self.ws.lyst.request("POST", f"{self.ws.base}/webhook-deliveries/{delivery_id}/redeliver")


class Workspace:
    def __init__(self, lyst: Lyst, ws: str) -> None:
        self.lyst = lyst
        self.id = ws
        self.base = f"/v1/workspaces/{ws}"
        self.files = _Files(self)
        self.events = _Events(self)
        self.webhooks = _Webhooks(self)

    def datasets(self) -> list[dict[str, Any]]:
        return self.lyst.get(f"{self.base}/datasets")

    def metrics(self) -> list[dict[str, Any]]:
        return self.lyst.get(f"{self.base}/metrics")

    def sql(self, sql: str, *, max_rows: int = 1000) -> QueryResult:
        r = self.lyst.request("POST", f"{self.base}/query", json={"sql": sql, "max_rows": max_rows}, idempotent=True)
        return QueryResult(r["columns"], r["rows"], r["executed_sql"], r["ms"], r.get("datasets", []))

    def submit(self, question: str, *, autonomy: str | None = None, idempotency_key: str | None = None) -> str:
        """Start an analysis. The idempotency key makes a retried submit return the same task, never a second run."""
        key = idempotency_key or uuid.uuid4().hex
        r = self.lyst.request("POST", f"{self.base}/tasks", json={"question": question, "autonomy": autonomy},
                              headers={"idempotency-key": key}, idempotent=True)
        return r["task_id"]

    def ask(self, question: str, *, autonomy: str | None = None, timeout: float = 600) -> dict[str, Any]:
        return self.lyst.wait(self.submit(question, autonomy=autonomy), timeout=timeout)

    def tasks(self) -> list[dict[str, Any]]:
        return self.lyst.get(f"{self.base}/tasks")

    def search(self, q: str, *, limit: int = 20) -> Any:
        return self.lyst.get(f"{self.base}/catalog/search", q=q, limit=limit)
