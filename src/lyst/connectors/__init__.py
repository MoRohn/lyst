# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Connector SDK: isolated, least-privilege access to source systems, declared by a manifest.

A connector class declares a ``Manifest`` (kind, label, the config fields it needs, which are secret,
what it can do) and implements ``test``, ``discover`` and ``read``. The ``ConnectionService`` stores
connections per workspace (secrets in their own column of the encrypted control database, never
returned by the API), tests before saving, discovers tables, and syncs a source table into a governed
dataset: a full load the first time, then only rows past the cursor column's last value. Every sync is
a new dataset version (earlier versions stay reproducible) and an audit line.

Connectors read. None of them writes to a source: the PostgreSQL connector opens every transaction
``READ ONLY`` and sets a statement timeout. A new connector is a module in this package that registers
its class in ``CONNECTORS``; the core application does not change.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from ..security import Guard, Principal
from ..store import Stores


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    default: str | None = None


@dataclass(frozen=True)
class Manifest:
    kind: str
    label: str
    fields: tuple[Field, ...]
    capabilities: tuple[str, ...] = ("test", "discover", "read", "incremental")
    notes: str = ""
    auth: str = "none"            # none | password | token | api_key: what kind of secret the connection holds
    read_only: bool = True        # connectors never write to a source
    retry_attempts: int = 3       # attempts per operation for transient failures (network, timeout, rate limit, source)


@dataclass
class SourceTable:
    schema: str
    name: str
    columns: list[dict[str, str]]
    rows_estimate: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


class ConnectorError(RuntimeError):
    """A connector failure. ``category`` (see ``reliability.CATEGORIES``) is set by connectors that know it, such as an
    HTTP 429 with ``retry_after`` seconds; otherwise it is inferred from the message."""

    def __init__(self, message: str = "", *, category: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.category, self.retry_after = category, retry_after


class Connector:
    manifest: Manifest

    def __init__(self, config: dict[str, Any], secrets: dict[str, str]):
        self.config, self.secrets = config, secrets

    def test(self) -> dict[str, Any]:
        raise NotImplementedError

    def discover(self) -> list[SourceTable]:
        raise NotImplementedError

    def read(self, table: str, *, cursor_column: str | None = None, after: Any = None,
             batch_rows: int = 50_000) -> Iterator[pa.Table]:
        raise NotImplementedError


CONNECTORS: dict[str, type[Connector]] = {}


def scrub(text: str, secrets: dict[str, Any]) -> str:
    """Remove a connection's own secret values (raw and URL-encoded) from text. Pattern redaction cannot know that
    an arbitrary password is a secret; the connector service can, because it holds the values for the call."""
    from urllib.parse import quote, quote_plus  # noqa: PLC0415

    for v in sorted((str(v) for v in secrets.values() if v), key=len, reverse=True):
        for form in {v, quote(v, safe=""), quote_plus(v)}:
            if len(form) < 4:
                # a short value (a PIN) is removed only where it stands alone, so ordinary words are not mangled
                text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", "[secret]", text)
            else:
                text = text.replace(form, "[secret]")
    return text


class _Scrubbed(Connector):
    """Wraps a connector so no error it raises can carry its secrets into a stored status, an API response, a log
    line or a chained traceback: every failure becomes a ConnectorError with the values removed."""

    def __init__(self, inner: Connector, secrets: dict[str, Any]):  # noqa: D107 - no super(): config lives on inner
        self.inner, self._secrets = inner, secrets
        self.manifest = inner.manifest
        self.config, self.secrets = inner.config, inner.secrets

    def _fail(self, exc: BaseException) -> ConnectorError:
        from .reliability import classify  # noqa: PLC0415

        return ConnectorError(scrub(str(exc) or type(exc).__name__, self._secrets), category=classify(exc),
                              retry_after=getattr(exc, "retry_after", None))

    def test(self) -> dict[str, Any]:
        try:
            return self.inner.test()
        except Exception as exc:  # noqa: BLE001 - re-raised, scrubbed, without the original as cause
            raise self._fail(exc) from None

    def discover(self) -> list[SourceTable]:
        try:
            return self.inner.discover()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None

    def read(self, table: str, **kw: Any) -> Iterator[pa.Table]:
        try:
            yield from self.inner.read(table, **kw)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None


def connector_class(kind: str) -> type[Connector]:
    """The connector registered for ``kind``; an unknown kind is a ConnectorError (a 400), wherever it is looked up."""
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ConnectorError(f"unknown connector kind {kind}")
    return cls


def register(cls: type[Connector]) -> type[Connector]:
    CONNECTORS[cls.manifest.kind] = cls
    return cls


def manifests() -> list[dict[str, Any]]:
    return [{"kind": c.manifest.kind, "label": c.manifest.label, "capabilities": list(c.manifest.capabilities),
             "notes": c.manifest.notes, "auth": c.manifest.auth, "read_only": c.manifest.read_only, "retry_attempts": c.manifest.retry_attempts,
             "fields": [f.__dict__ for f in c.manifest.fields]} for c in CONNECTORS.values()]


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def validate_identifier(value: str, what: str = "identifier") -> str:
    if not _NAME.match(value or ""):
        raise ConnectorError(f"invalid {what}: {value!r}")
    return value


# ---------------------------------------------------------------------------- the service


@dataclass
class SyncResult:
    connection_id: str
    source_table: str
    dataset: str
    version: int
    rows_added: int
    rows_total: int
    cursor: Any
    seconds: float
    mode: str
    extra: dict[str, Any] = field(default_factory=dict)


class ConnectionService:
    def __init__(self, stores: Stores, guard: Guard, data, credentials=None) -> None:
        self.stores, self.guard, self.data, self.credentials = stores, guard, data, credentials
        self.sleep = time.sleep   # retry backoff; tests replace it
        self.catalog = None       # set by the composition root: health reads dataset freshness and contract state
        if credentials is not None:
            credentials.connector_rotate = self._rotate_secret

    def _rotate_secret(self, credential_id: str, kind: str, new_value: str) -> tuple[dict[str, str], dict[str, Any]]:
        """Replace the one secret field of the connections that use `credential_id`, test each connector with it, and
        return the new payload. Called by the credential service's Replace (which has already checked that the caller
        administers the credential's workspace), so Settings can rotate a password in place."""
        rows = self.stores.control.query("SELECT id, workspace_id, config FROM connections WHERE credential_id = ? ORDER BY id",
                                         [credential_id])
        if not rows:
            raise ValueError("no connection uses this credential any more")
        if len({r["workspace_id"] for r in rows}) != 1:
            raise ValueError("this credential is used by connections in more than one workspace; recreate them on the Data page")
        cls = connector_class(kind)
        secret_fields = [f.name for f in cls.manifest.fields if f.secret]
        if len(secret_fields) != 1:
            raise ValueError("this connection has more than one secret field; recreate it on the Data page")
        current = self.credentials.connector_secrets(credential_id, workspace=rows[0]["workspace_id"])
        payload = {**current, secret_fields[0]: new_value}
        result: Any = None
        for r in rows:  # every connection sharing the credential must work with the new value
            try:
                result = self._connector(kind, json.loads(r["config"]), payload).test()
            except Exception as exc:  # noqa: BLE001 - the connector says why; never the value
                return payload, {"ok": False, "outcome": f"connection test failed: {str(exc)[:160]}"}
        for r in rows:
            self.stores.control.execute("UPDATE connections SET status = 'ok', last_tested_at = now(), last_error = NULL, "
                                        "failure_category = NULL, consecutive_failures = 0 WHERE id = ?", [r["id"]])
        return payload, {"ok": True, "outcome": "connection tested", **({"detail": result} if isinstance(result, dict) else {})}

    @staticmethod
    def _split(kind: str, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cls = connector_class(kind)
        config, secrets = {}, {}
        for f in cls.manifest.fields:
            v = values.get(f.name, f.default)
            if f.required and (v is None or v == ""):
                raise ConnectorError(f"{f.label} is required")
            (secrets if f.secret else config)[f.name] = v
        return config, secrets

    def _connector(self, kind: str, config: dict[str, Any], secrets: dict[str, str]) -> Connector:
        try:
            inner = connector_class(kind)(config, secrets)
        except ConnectorError as exc:
            raise ConnectorError(scrub(str(exc), secrets), category=exc.category) from None
        except Exception as exc:  # noqa: BLE001 - a constructor that validates may echo what it was given
            raise ConnectorError(scrub(str(exc), secrets)) from None
        return _Scrubbed(inner, secrets)

    def _resolve_secrets(self, ws: str, credential_id: str | None, inline: dict[str, str]) -> dict[str, str]:
        """The connector worker is the only consumer that sees a connection's secrets, for the duration of the call."""
        if credential_id and self.credentials is not None:
            got = self.credentials.connector_secrets(credential_id, workspace=ws)
            return {**inline, **got}
        return inline

    def create(self, actor: Principal, ws: str, *, kind: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.ingest", name)
        values = dict(values)
        credential_id = values.pop("credential_id", None) or None
        config, secrets = self._split(kind, values)
        secrets = {k: v for k, v in secrets.items() if v not in (None, "")}
        if credential_id:  # a credential connected in Settings supplies the secret (and host fields) instead of the form
            mapped = self.credentials.connector_secrets(credential_id, workspace=ws)
            for k, v in mapped.items():
                (secrets if any(f.name == k and f.secret for f in connector_class(kind).manifest.fields) else config)[k] = v
            secrets = {k: v for k, v in secrets.items() if v not in (None, "")}
        if secrets and not credential_id and self.credentials is None:
            # never keep a password as plain JSON on the connection row: secrets live in the credential store
            raise ConnectorError("this installation has no credential store, so a connection with a secret cannot be saved")
        result = self._connector(kind, config, secrets).test()  # raises on failure: nothing untestable is saved
        if not credential_id and secrets and self.credentials is not None:
            credential_id = self.credentials.store_connector_secrets(actor, ws, kind, name, secrets)
            secrets = {}
        cid = "c_" + uuid.uuid4().hex[:10]
        self.stores.control.execute(
            "INSERT INTO connections (id, workspace_id, kind, name, config, secrets, status, last_tested_at, created_by, credential_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', now(), ?, ?)",
            [cid, ws, kind, name, json.dumps(config), json.dumps({}), actor.user_id, credential_id])
        self.guard.audit(actor, ws, "connection.create", cid, "ok", {"kind": kind, "name": name, "credential": credential_id})
        return {"id": cid, "kind": kind, "name": name, "test": result, "credential_id": credential_id}

    def _load(self, actor: Principal, ws: str, cid: str) -> tuple[dict[str, Any], Connector]:
        rows = self.stores.control.query("SELECT * FROM connections WHERE id = ? AND workspace_id = ?", [cid, ws])
        if not rows:
            raise KeyError(cid)
        row = rows[0]
        try:
            secrets = self._resolve_secrets(ws, row.get("credential_id"), json.loads(row["secrets"]))
        except Exception as exc:  # a revoked or unreadable credential: the connection is in error, with a plain reason
            reason = f"credential unavailable: {str(exc)[:200]}"
            self._failed(actor, ws, row, "load", None, ConnectorError(reason, category="auth"), Attempted(attempts=1, failures=["auth"]), 0)
            raise ConnectorError(reason, category="auth") from exc
        return row, self._connector(row["kind"], json.loads(row["config"]), secrets)

    def list(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        rows = self.stores.control.query(
            "SELECT id, kind, name, config, status, last_tested_at, last_error, created_by, created_at, credential_id FROM connections "
            "WHERE workspace_id = ? ORDER BY name", [ws])
        for r in rows:
            r["config"] = json.loads(r["config"])  # secrets are a separate column and never leave the service
        return rows

    def test(self, actor: Principal, ws: str, cid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.ingest", cid)
        row, conn = self._load(actor, ws, cid)
        return self._attempt(actor, ws, row, "test", conn.test)

    # -- operations: retries, events, health ----------------------------------------------------

    def _attempt(self, actor: Principal, ws: str, row: dict[str, Any], action: str, fn, *, table: str | None = None,
                 details: dict[str, Any] | None = None):
        """Run one connector operation under the connector's retry policy, record it as an event, and keep the
        connection's health state current. A failure re-raises as a ConnectorError carrying its category."""
        cls = CONNECTORS.get(row["kind"])
        policy = RetryPolicy(attempts=cls.manifest.retry_attempts if cls else 3)
        record = Attempted()
        start = time.perf_counter()
        try:
            out = run_with_retry(fn, policy, sleep=self.sleep, record=record)
        except Exception as exc:  # noqa: BLE001 - classified, recorded and re-raised
            err = exc if isinstance(exc, ConnectorError) else ConnectorError(str(exc), category=classify(exc))
            self._failed(actor, ws, row, action, table, err, record, int((time.perf_counter() - start) * 1000), details)
            raise ConnectorError(str(err), category=classify(err)) from None
        self._event(ws, row["id"], action, "ok", None, None, record, int((time.perf_counter() - start) * 1000), table, details, actor)
        self.stores.control.execute(
            "UPDATE connections SET status = 'ok', last_tested_at = now(), last_success_at = now(), last_error = NULL, failure_category = NULL, "
            "consecutive_failures = 0 WHERE id = ?", [row["id"]])
        return out

    def _event(self, ws: str, cid: str, action: str, outcome: str, category: str | None, message: str | None, record: Attempted,
               ms: int, table: str | None, details: dict[str, Any] | None, actor: Principal | None) -> None:
        self.stores.control.execute(
            "INSERT INTO connection_events (id, connection_id, workspace_id, action, source_table, outcome, category, message, attempts, "
            "retried, ms, details, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["ce_" + uuid.uuid4().hex[:12], cid, ws, action, table, outcome, category, (message or "")[:300] or None, record.attempts,
             json.dumps(record.failures or []), ms, json.dumps(details or {}, default=str), actor.user_id if actor else None])

    def _failed(self, actor: Principal | None, ws: str, row: dict[str, Any], action: str, table: str | None, err: ConnectorError,
                record: Attempted, ms: int, details: dict[str, Any] | None = None) -> None:
        category = classify(err)
        message = str(err)[:300]
        self._event(ws, row["id"], action, "error", category, message, record, ms, table, details, actor)
        self.stores.control.execute(
            "UPDATE connections SET status = 'error', last_tested_at = now(), last_failure_at = now(), last_error = ?, failure_category = ?, "
            "consecutive_failures = consecutive_failures + 1 WHERE id = ?", [message, category, row["id"]])
        failures = int(self.stores.control.scalar("SELECT consecutive_failures FROM connections WHERE id = ?", [row["id"]]) or 0)
        if category not in TRANSIENT or failures >= FAILING_AFTER:
            from ..notices import raise_notice  # noqa: PLC0415

            raise_notice(self.stores, ws, source=f"connection:{row['id']}", kind="connection_needs_action",
                         label=row["name"], summary=f"The connection {row['name']} ({row['kind']}) is failing: {message}. "
                         f"{ACTIONS.get(category, ACTIONS['unknown'])}",
                         priority=8.0 if category in ("auth", "permission") else 6.0, period=category,
                         dedupe_key=f"connection:{row['id']}:{category}:{time.strftime('%Y-%m-%d')}", recipients={row.get("created_by")})

    def events(self, actor: Principal, ws: str, cid: str, *, limit: int = 50) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read", cid)
        if not self.stores.control.scalar("SELECT count(*) FROM connections WHERE id = ? AND workspace_id = ?", [cid, ws]):
            raise KeyError(cid)
        rows = self.stores.control.query("SELECT id, action, source_table, outcome, category, message, attempts, retried, ms, details, actor, "
                                         "occurred_at FROM connection_events WHERE connection_id = ? AND workspace_id = ? "
                                         "ORDER BY occurred_at DESC LIMIT ?", [cid, ws, max(1, min(int(limit), 500))])
        for r in rows:
            r["retried"], r["details"] = json.loads(r["retried"] or "[]"), json.loads(r["details"] or "{}")
        return rows

    def health(self, actor: Principal, ws: str, cid: str | None = None) -> list[dict[str, Any]]:
        """Per connection: a state (healthy, degraded, failing, needs_action, untested), why, what to do, last success and
        failure, each synced table's freshness, schedule and contract, and the last day's errors, retries and rate limits.
        Never a secret: credentials appear by id and state only."""
        self.guard.require(actor, ws, "data.read")
        ctl = self.stores.control
        sql = ("SELECT id, kind, name, status, last_tested_at, last_success_at, last_failure_at, last_error, failure_category, "
               "consecutive_failures, credential_id, created_by FROM connections WHERE workspace_id = ?")
        rows = ctl.query(sql + (" AND id = ?" if cid else "") + " ORDER BY name", [ws, cid] if cid else [ws])
        if cid and not rows:
            raise KeyError(cid)
        schedules = ctl.query("SELECT id, request, every_seconds, enabled, next_run_at FROM schedules WHERE workspace_id = ?", [ws])
        overview: dict[str, dict[str, Any]] = {}
        if self.catalog is not None:
            try:
                overview = {d["name"]: d for d in self.catalog.overview(actor, ws)}
            except Exception:  # noqa: BLE001 - freshness is extra detail on a health row
                logging.getLogger("lyst.connectors").exception("catalog overview for connection health failed")
        # one query per kind of detail for every connection, grouped here (not three or more per connection)
        ids = [r["id"] for r in rows]
        marks = ", ".join("?" for _ in ids) or "NULL"
        events_by: dict[str, list[dict[str, Any]]] = {}
        for e in ctl.query(f"SELECT connection_id, outcome, category, attempts, retried FROM connection_events WHERE connection_id IN ({marks}) "
                           "AND action <> 'schema_change' AND occurred_at > now() - INTERVAL 1 DAY", ids):
            events_by.setdefault(e["connection_id"], []).append(e)
        cred_ids = sorted({r["credential_id"] for r in rows if r["credential_id"]})
        creds = {c["id"]: c for c in ctl.query(
            "SELECT id, status, version, last_used_at, replaced_at, revoked_at FROM credentials WHERE id IN "
            f"({', '.join('?' for _ in cred_ids) or 'NULL'})", cred_ids)}
        syncs_by: dict[str, list[dict[str, Any]]] = {}
        for t in ctl.query("SELECT connection_id, source_table, dataset, cursor_column, last_sync_at, last_rows_added, rows_total, syncs, "
                           "last_schema, epoch(now() - last_sync_at) AS age FROM connection_syncs "
                           f"WHERE connection_id IN ({marks}) ORDER BY source_table", ids):
            syncs_by.setdefault(t["connection_id"], []).append(t)
        questions = [(json.loads(s["request"]).get("question", ""), s) for s in schedules]  # decoded once, not per table
        out = []
        for r in rows:
            day = events_by.get(r["id"], [])
            stats = {"operations": len(day), "errors": sum(e["outcome"] == "error" for e in day),
                     "retries": sum(max(0, (e["attempts"] or 1) - 1) for e in day),
                     "rate_limited": sum("rate_limit" in json.loads(e["retried"] or "[]") for e in day)}
            credential = None
            if r["credential_id"]:
                credential = creds.get(r["credential_id"]) or {"id": r["credential_id"], "status": "missing"}
            tables = []
            for t in syncs_by.get(r["id"], []):
                prefix = f"sync connection {r['id']} table {t['source_table']}"
                sched = next((s for q, s in questions if q.startswith(prefix)), None)
                age = t["age"] if t["last_sync_at"] else None
                overdue = bool(sched and sched["enabled"] and age is not None and age > 2 * sched["every_seconds"])
                entry = {"source_table": t["source_table"], "dataset": t["dataset"], "mode": "incremental" if t["cursor_column"] else "full",
                         "last_sync_at": t["last_sync_at"], "last_rows_added": t["last_rows_added"], "rows": t["rows_total"], "syncs": t["syncs"],
                         "schedule": {"every_seconds": sched["every_seconds"], "enabled": sched["enabled"], "next_run_at": sched["next_run_at"]}
                         if sched else None, "overdue": overdue}
                ov = overview.get(t["dataset"])
                if ov:
                    entry["data_freshness"] = ov["freshness"]
                    entry["contract"] = ov["contract"]
                tables.append(entry)
            reasons: list[str] = []
            category = r["failure_category"]
            if r["status"] == "error" and category and category not in TRANSIENT:
                state = "needs_action"
                reasons.append(f"last {r['last_failure_at']:%Y-%m-%d %H:%M} failed: {r['last_error']}" if r["last_failure_at"] else r["last_error"] or "")
            elif r["status"] == "error" and (r["consecutive_failures"] or 0) >= FAILING_AFTER:
                state = "failing"
                reasons.append(f"{r['consecutive_failures']} operations in a row failed ({category}): {r['last_error']}")
            elif r["status"] == "error":
                state = "degraded"
                reasons.append(f"the last operation failed ({category}) and will be retried: {r['last_error']}")
            elif not r["last_success_at"] and not r["last_tested_at"]:
                state = "untested"
            else:
                state = "healthy"
            if credential and credential.get("status") not in ("active", None):
                state = "needs_action"
                category = "auth"
                reasons.append(f"its credential is {credential['status']}")
            for t in tables:
                if t["overdue"]:
                    state = "degraded" if state == "healthy" else state
                    reasons.append(f"the scheduled sync of {t['source_table']} is overdue")
                if (t.get("contract") or {}).get("outcome") in ("breaking", "fail"):
                    state = "degraded" if state == "healthy" else state
                    reasons.append(f"{t['dataset']} {'breaks' if t['contract']['outcome'] == 'breaking' else 'fails'} its data contract")
            if stats["rate_limited"] and state == "healthy":
                state = "degraded"
                reasons.append(f"the source rate-limited {stats['rate_limited']} operation{'s' if stats['rate_limited'] != 1 else ''} today")
            action = None
            if state in ("needs_action", "failing", "degraded"):
                if category:
                    action = ACTIONS.get(category, ACTIONS["unknown"])
                elif any(t["overdue"] for t in tables):
                    action = "Check that the Lyst worker is running; scheduled syncs run there."
                elif any((t.get("contract") or {}).get("outcome") in ("breaking", "fail") for t in tables):
                    action = ACTIONS["schema"]
                else:
                    action = ACTIONS["rate_limit"]
            elif state == "untested":
                action = "Test the connection."
            out.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "state": state, "reasons": reasons, "required_action": action,
                        "failure_category": category if state != "healthy" else None, "last_success_at": r["last_success_at"],
                        "last_failure_at": r["last_failure_at"], "consecutive_failures": r["consecutive_failures"] or 0,
                        "credential": credential, "tables": tables, "last_24h": stats})
        return out

    def delete(self, actor: Principal, ws: str, cid: str) -> None:
        self.guard.require(actor, ws, "workspace.admin", cid)
        rows = self.stores.control.query("SELECT credential_id FROM connections WHERE id = ? AND workspace_id = ?", [cid, ws])
        if not rows:  # not in this workspace: a 404, never an audited "ok" for a delete that removed nothing
            raise KeyError(cid)
        cred = rows[0]["credential_id"]
        if cred and self.credentials is not None:
            self.credentials.delete_connector_secrets(actor, ws, cred)
        self.stores.control.execute("DELETE FROM connections WHERE id = ? AND workspace_id = ?", [cid, ws])
        self.guard.audit(actor, ws, "connection.delete", cid, "ok")

    def discover(self, actor: Principal, ws: str, cid: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read", cid)
        row, conn = self._load(actor, ws, cid)
        return [{"ref": t.ref, "schema": t.schema, "name": t.name, "columns": t.columns, "rows_estimate": t.rows_estimate}
                for t in self._attempt(actor, ws, row, "discover", conn.discover)]

    def syncs(self, actor: Principal, ws: str, cid: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        sql = "SELECT * FROM connection_syncs WHERE workspace_id = ?"
        params: list[Any] = [ws]
        if cid:
            sql += " AND connection_id = ?"
            params.append(cid)
        return self.stores.control.query(sql + " ORDER BY last_sync_at DESC", params)

    def sync(self, actor: Principal, ws: str, cid: str, *, source_table: str, dataset: str | None = None,
             cursor_column: str | None = None, task_id: str | None = None) -> SyncResult:
        """Full load the first time; afterwards only rows with cursor_column > the last value seen."""
        self.guard.require(actor, ws, "data.ingest", cid)
        validate_identifier(source_table, "table")
        if cursor_column:
            validate_identifier(cursor_column, "cursor column")
        row, conn = self._load(actor, ws, cid)
        from ..data import normalise_name  # noqa: PLC0415

        # the name the dataset is stored under, so the sync state and every lookup below use the same one
        dataset = normalise_name(dataset or re.sub(r"[^a-z0-9_]+", "_", source_table.split(".")[-1].lower()))
        state = self.stores.control.query(
            "SELECT * FROM connection_syncs WHERE connection_id = ? AND source_table = ?", [cid, source_table])
        last_cursor = None
        if state and cursor_column and state[0]["cursor_column"] == cursor_column and state[0]["last_cursor"] is not None:
            last_cursor = json.loads(state[0]["last_cursor"])
        mode = "incremental" if last_cursor is not None else "full"
        start = time.perf_counter()
        spool = _Spool()
        try:
            # batches go to disk as they arrive: memory holds one batch, never the whole source table
            self._attempt(actor, ws, row, "read", lambda: spool.fill(conn.read(source_table, cursor_column=cursor_column,
                                                                              after=last_cursor), cursor_column),
                          table=source_table)
            change = _schema_change(self.stores.workspace(ws), dataset, spool.schema)
            if change and change["retyped"] and mode == "incremental":
                # rows already stored cannot take a new column type: reload in full so the version is consistent
                mode = "full"
                self._attempt(actor, ws, row, "read", lambda: spool.fill(conn.read(source_table, cursor_column=cursor_column,
                                                                                  after=None), cursor_column),
                              table=source_table, details={"reason": "schema changed; reloading in full"})
            rows_added = spool.rows
            if rows_added == 0 and mode == "incremental":
                self.stores.control.execute("UPDATE connection_syncs SET last_sync_at = now(), last_rows_added = 0, syncs = syncs + 1 "
                                            "WHERE connection_id = ? AND source_table = ?", [cid, source_table])
                total = self.stores.workspace(ws).scalar(
                    "SELECT v.row_count FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version "
                    "WHERE d.name = ?", [dataset]) or 0
                return SyncResult(cid, source_table, dataset, int(self.stores.workspace(ws).scalar(
                    "SELECT current_version FROM datasets WHERE name = ?", [dataset]) or 0), 0, int(total), last_cursor,
                    time.perf_counter() - start, "incremental")
            source = spool.dataset() if spool.files else pa.table({})
            result = self.data.ingest_arrow(actor, ws, dataset, source, source=f"{row['kind']}:{row['name']}:{source_table}",
                                            sha=spool.sha256() if spool.files else None,
                                            append_to_previous=(mode == "incremental"), title=source_table.split(".")[-1].replace("_", " ").title(),
                                            description=f"Synced from {row['name']} ({row['kind']}) table {source_table}")
            new_schema = spool.schema
            new_cursor = last_cursor
            if cursor_column and rows_added and spool.max_cursor is not None:
                mx = spool.max_cursor
                new_cursor = mx.isoformat() if hasattr(mx, "isoformat") else mx
        except ConnectorError as err:
            # a failed sync is news too: the event stream (and rules such as "flag a failing connection") see it
            self.guard.audit(actor, ws, "connection.sync", cid, "error", {"table": source_table, "dataset": dataset,
                                                                          "mode": mode, "category": classify(err)})
            raise
        finally:
            spool.close()
        last_schema = json.dumps({f.name: str(f.type) for f in new_schema}) if new_schema is not None and len(new_schema) else None
        # last_schema is carried over when this sync saw no columns: the drift baseline is never reset to NULL
        self.stores.control.execute(
            "INSERT OR REPLACE INTO connection_syncs (connection_id, workspace_id, source_table, dataset, cursor_column, last_cursor, "
            "last_sync_at, last_rows_added, rows_total, syncs, last_schema) VALUES (?, ?, ?, ?, ?, ?, now(), ?, ?, "
            "coalesce((SELECT syncs FROM connection_syncs WHERE connection_id = ? AND source_table = ?), 0) + 1, "
            "coalesce(?, (SELECT last_schema FROM connection_syncs WHERE connection_id = ? AND source_table = ?)))",
            [cid, ws, source_table, dataset, cursor_column, json.dumps(new_cursor, default=str), rows_added, result["rows"], cid, source_table,
             last_schema, cid, source_table])
        if change:
            self._event(ws, cid, "schema_change", "ok", "schema", change["text"], Attempted(attempts=1), 0, source_table, change, actor)
        self.guard.audit(actor, ws, "connection.sync", cid, "ok", {"table": source_table, "dataset": dataset, "mode": mode,
                                                                   "rows_added": rows_added, "schema_change": bool(change)})
        return SyncResult(cid, source_table, dataset, result["version"], rows_added, result["rows"], new_cursor,
                          time.perf_counter() - start, mode, {"schema_change": change} if change else {})


class _Spool:
    """A source read written to Parquet files on disk batch by batch, so a sync holds one batch in memory at a time.

    Column names are normalised as they are written; the batches' schemas are unified (a column missing from a page
    is null there, an int that later turns float is widened), and the result is scanned lazily as one dataset.
    """

    def __init__(self) -> None:
        import tempfile  # noqa: PLC0415

        self._dir = tempfile.TemporaryDirectory(prefix="lyst-sync-")
        self.files: list[str] = []
        self.schemas: list[pa.Schema] = []
        self.rows = 0
        self.max_cursor: Any = None

    def fill(self, batches: Iterator[pa.Table], cursor_column: str | None) -> _Spool:
        import os  # noqa: PLC0415

        import pyarrow.compute as pc  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415

        from ..data import unique_names  # noqa: PLC0415

        for name in self.files:  # a retried read starts again from nothing
            os.unlink(name)
        self.files, self.schemas, self.rows, self.max_cursor = [], [], 0, None
        for batch in batches:
            if not batch.num_rows:
                continue
            if cursor_column and cursor_column in batch.column_names:
                mx = pc.max(batch[cursor_column]).as_py()
                if mx is not None and (self.max_cursor is None or _later(mx, self.max_cursor)):
                    self.max_cursor = mx
            batch = batch.rename_columns(unique_names(batch.column_names))
            path = os.path.join(self._dir.name, f"{len(self.files):06d}.parquet")
            pq.write_table(batch, path)
            self.files.append(path)
            self.schemas.append(batch.schema)
            self.rows += batch.num_rows
        return self

    @property
    def schema(self) -> pa.Schema | None:
        return pa.unify_schemas(self.schemas, promote_options="permissive") if self.schemas else None

    def dataset(self):
        import pyarrow.dataset as ds  # noqa: PLC0415

        return ds.dataset(self.files, schema=self.schema, format="parquet")

    def sha256(self) -> str:
        import hashlib  # noqa: PLC0415

        h = hashlib.sha256()
        for name in self.files:
            with open(name, "rb") as f:
                for chunk in iter(lambda f=f: f.read(1 << 20), b""):
                    h.update(chunk)
        return h.hexdigest()

    def close(self) -> None:
        self._dir.cleanup()


def _later(a: Any, b: Any) -> bool:
    """``a > b`` for cursor values, which a loosely typed source (JSON) can mix: compared as text when types differ."""
    try:
        return a > b
    except TypeError:
        return str(a) > str(b)


def _arrow_family(t: pa.DataType) -> str:
    if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "number"
    if pa.types.is_timestamp(t) or pa.types.is_date(t) or pa.types.is_time(t):
        return "time"
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_null(t):
        return "null"
    return "text"


def _schema_change(db, dataset: str, new: pa.Schema | None) -> dict[str, Any] | None:
    """Columns added, removed or retyped between the dataset's current version and what the source returned now."""
    if new is None or not len(new):
        return None
    current = db.query("SELECT v.schema FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version "
                       "WHERE d.name = ?", [dataset])
    if not current:
        return None
    from ..catalog import _family  # noqa: PLC0415
    from ..data import normalise_name  # noqa: PLC0415

    before = {c["name"]: _family(c["type"]) for c in json.loads(current[0]["schema"])}
    after = {normalise_name(f.name): _arrow_family(f.type) for f in new}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    retyped = sorted(c for c in set(before) & set(after) if after[c] not in ("null", before[c]))
    if not (added or removed or retyped):
        return None
    parts = [f"added {', '.join(added)}" if added else "", f"removed {', '.join(removed)}" if removed else "",
             f"changed type of {', '.join(retyped)}" if retyped else ""]
    return {"added": added, "removed": removed, "retyped": retyped,
            "text": f"{dataset}: the source " + "; ".join(p for p in parts if p)}


FAILING_AFTER = 3  # consecutive transient failures before a connection counts as failing and its owners are told

from . import files, http_file, postgres, rest, sqlite  # noqa: E402,F401 - registers the connectors
from .reliability import ACTIONS, TRANSIENT, Attempted, RetryPolicy, classify, run_with_retry  # noqa: E402

__all__ = ["CONNECTORS", "ConnectionService", "Connector", "ConnectorError", "Field", "Manifest", "SourceTable",
           "SyncResult", "manifests", "register"]
