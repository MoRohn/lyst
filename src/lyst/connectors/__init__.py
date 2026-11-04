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
    pass


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
        if len(v) < 4:
            continue
        for form in {v, quote(v, safe=""), quote_plus(v)}:
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
        return ConnectorError(scrub(str(exc) or type(exc).__name__, self._secrets))

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


def register(cls: type[Connector]) -> type[Connector]:
    CONNECTORS[cls.manifest.kind] = cls
    return cls


def manifests() -> list[dict[str, Any]]:
    return [{"kind": c.manifest.kind, "label": c.manifest.label, "capabilities": list(c.manifest.capabilities),
             "notes": c.manifest.notes,
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
        if credentials is not None:
            credentials.connector_rotate = self._rotate_secret

    def _rotate_secret(self, credential_id: str, kind: str, new_value: str) -> tuple[dict[str, str], dict[str, Any]]:
        """Replace the one secret field of the connection that uses `credential_id`, test the connector with it, and
        return the new payload. Called by the credential service's Replace, so Settings can rotate a password in place."""
        row = self.stores.control.query("SELECT id, workspace_id, config FROM connections WHERE credential_id = ?", [credential_id])
        if not row:
            raise ValueError("no connection uses this credential any more")
        cls = CONNECTORS.get(kind)
        if not cls:
            raise ValueError(f"unknown connector kind {kind}")
        secret_fields = [f.name for f in cls.manifest.fields if f.secret]
        if len(secret_fields) != 1:
            raise ValueError("this connection has more than one secret field; recreate it on the Data page")
        current = self.credentials.connector_secrets(credential_id, workspace=row[0]["workspace_id"])
        payload = {**current, secret_fields[0]: new_value}
        try:
            result = self._connector(kind, json.loads(row[0]["config"]), payload).test()
        except Exception as exc:  # noqa: BLE001 - the connector says why; never the value
            return payload, {"ok": False, "outcome": f"connection test failed: {str(exc)[:160]}"}
        self.stores.control.execute("UPDATE connections SET status = 'ok', last_tested_at = now(), last_error = NULL WHERE id = ?", [row[0]["id"]])
        return payload, {"ok": True, "outcome": "connection tested", **({"detail": result} if isinstance(result, dict) else {})}

    @staticmethod
    def _split(kind: str, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        cls = CONNECTORS.get(kind)
        if not cls:
            raise ConnectorError(f"unknown connector kind {kind}")
        config, secrets = {}, {}
        for f in cls.manifest.fields:
            v = values.get(f.name, f.default)
            if f.required and (v is None or v == ""):
                raise ConnectorError(f"{f.label} is required")
            (secrets if f.secret else config)[f.name] = v
        return config, secrets

    def _connector(self, kind: str, config: dict[str, Any], secrets: dict[str, str]) -> Connector:
        try:
            inner = CONNECTORS[kind](config, secrets)
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
                (secrets if any(f.name == k and f.secret for f in CONNECTORS[kind].manifest.fields) else config)[k] = v
            secrets = {k: v for k, v in secrets.items() if v not in (None, "")}
        result = self._connector(kind, config, secrets).test()  # raises on failure: nothing untestable is saved
        if not credential_id and secrets and self.credentials is not None:
            credential_id = self.credentials.store_connector_secrets(actor, ws, kind, name, secrets)
            secrets = {}
        cid = "c_" + uuid.uuid4().hex[:10]
        self.stores.control.execute(
            "INSERT INTO connections (id, workspace_id, kind, name, config, secrets, status, last_tested_at, created_by, credential_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok', now(), ?, ?)",
            [cid, ws, kind, name, json.dumps(config), json.dumps(secrets if self.credentials is None else {}), actor.user_id, credential_id])
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
            self.stores.control.execute("UPDATE connections SET status = 'error', last_tested_at = now(), last_error = ? WHERE id = ?", [reason, cid])
            raise ConnectorError(reason) from exc
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
        _, conn = self._load(actor, ws, cid)
        try:
            result = conn.test()
            self.stores.control.execute("UPDATE connections SET status = 'ok', last_tested_at = now(), last_error = NULL WHERE id = ?", [cid])
            return result
        except Exception as exc:
            self.stores.control.execute("UPDATE connections SET status = 'error', last_tested_at = now(), last_error = ? WHERE id = ?",
                                        [str(exc)[:300], cid])
            raise ConnectorError(str(exc)) from exc

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
        _, conn = self._load(actor, ws, cid)
        return [{"ref": t.ref, "schema": t.schema, "name": t.name, "columns": t.columns, "rows_estimate": t.rows_estimate}
                for t in conn.discover()]

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
        dataset = dataset or re.sub(r"[^a-z0-9_]+", "_", source_table.split(".")[-1].lower())
        state = self.stores.control.query(
            "SELECT * FROM connection_syncs WHERE connection_id = ? AND source_table = ?", [cid, source_table])
        last_cursor = None
        if state and cursor_column and state[0]["cursor_column"] == cursor_column and state[0]["last_cursor"] is not None:
            last_cursor = json.loads(state[0]["last_cursor"])
        mode = "incremental" if last_cursor is not None else "full"
        start = time.perf_counter()
        batches = list(conn.read(source_table, cursor_column=cursor_column, after=last_cursor))
        new = pa.concat_tables(batches) if batches else None
        rows_added = new.num_rows if new is not None else 0
        if rows_added == 0 and mode == "incremental":
            self.stores.control.execute("UPDATE connection_syncs SET last_sync_at = now(), last_rows_added = 0, syncs = syncs + 1 "
                                        "WHERE connection_id = ? AND source_table = ?", [cid, source_table])
            total = self.stores.workspace(ws).scalar(
                "SELECT v.row_count FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version "
                "WHERE d.name = ?", [dataset]) or 0
            return SyncResult(cid, source_table, dataset, int(self.stores.workspace(ws).scalar(
                "SELECT current_version FROM datasets WHERE name = ?", [dataset]) or 0), 0, int(total), last_cursor,
                time.perf_counter() - start, "incremental")
        if new is None:
            new = pa.table({})
        result = self.data.ingest_arrow(actor, ws, dataset, new, source=f"{row['kind']}:{row['name']}:{source_table}",
                                        append_to_previous=(mode == "incremental"), title=source_table.split(".")[-1].replace("_", " ").title(),
                                        description=f"Synced from {row['name']} ({row['kind']}) table {source_table}")
        new_cursor = last_cursor
        if cursor_column and rows_added and cursor_column in new.column_names:
            import pyarrow.compute as pc  # noqa: PLC0415

            mx = pc.max(new[cursor_column]).as_py()
            new_cursor = mx.isoformat() if hasattr(mx, "isoformat") else mx
        self.stores.control.execute(
            "INSERT OR REPLACE INTO connection_syncs (connection_id, workspace_id, source_table, dataset, cursor_column, last_cursor, "
            "last_sync_at, last_rows_added, rows_total, syncs) VALUES (?, ?, ?, ?, ?, ?, now(), ?, ?, "
            "coalesce((SELECT syncs FROM connection_syncs WHERE connection_id = ? AND source_table = ?), 0) + 1)",
            [cid, ws, source_table, dataset, cursor_column, json.dumps(new_cursor, default=str), rows_added, result["rows"], cid, source_table])
        self.guard.audit(actor, ws, "connection.sync", cid, "ok", {"table": source_table, "dataset": dataset, "mode": mode,
                                                                   "rows_added": rows_added})
        return SyncResult(cid, source_table, dataset, result["version"], rows_added, result["rows"], new_cursor,
                          time.perf_counter() - start, mode)


from . import files, http_file, postgres, rest, sqlite  # noqa: E402,F401 - registers the connectors

__all__ = ["CONNECTORS", "ConnectionService", "Connector", "ConnectorError", "Field", "Manifest", "SourceTable",
           "SyncResult", "manifests", "register"]
