# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Governed datasets: versioned ingestion, catalog, profiling and guarded queries."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from ..analytics.profile import profile as profile_frame
from ..security import RANK, Guard, Principal
from ..store import Stores
from .sqlguard import GuardPolicy, QueryRejected, check_read_query

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def normalise_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", text.strip().lower()).strip("_")
    if not name or not name[0].isalpha():
        name = "t_" + name
    return name[:63]


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    sql: str
    executed_sql: str
    datasets: list[str]
    ms: int
    query_id: int

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)


class DataService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard

    # -- ingestion --------------------------------------------------------------------------

    def ingest(
        self,
        actor: Principal,
        workspace_id: str,
        name: str,
        content: bytes,
        *,
        filename: str,
        title: str | None = None,
        description: str | None = None,
        classification: str = "internal",
    ) -> dict[str, Any]:
        """Load a CSV or Parquet file as a new version of a dataset. Earlier versions stay."""
        try:
            table = _read_table(content, filename)
        except (pa.ArrowInvalid, pa.ArrowTypeError, UnicodeDecodeError) as exc:
            # a corrupt or mis-typed file is the person's input to fix, said plainly (HTTP 400), never a server error
            raise ValueError(f"{filename} could not be read: {str(exc).splitlines()[0][:200]}. Nothing was loaded; "
                             "check the file's format, delimiter and encoding (UTF-8), then upload it again.") from exc
        return self.ingest_arrow(actor, workspace_id, name, table, source=filename, sha=hashlib.sha256(content).hexdigest(),
                                 title=title, description=description, classification=classification)

    def ingest_arrow(
        self,
        actor: Principal,
        workspace_id: str,
        name: str,
        table: pa.Table,
        *,
        source: str,
        sha: str | None = None,
        title: str | None = None,
        description: str | None = None,
        classification: str = "internal",
        append_to_previous: bool = False,
    ) -> dict[str, Any]:
        """A new dataset version from an Arrow table. With ``append_to_previous`` the new version is the current
        version's rows plus these (an incremental sync); otherwise it replaces them."""
        self.guard.require(actor, workspace_id, "data.ingest", name)
        name = normalise_name(name)
        if not _NAME.match(name):
            raise ValueError("invalid dataset name")
        table = table.rename_columns([normalise_name(c) for c in table.column_names])
        if sha is None:
            import io as _io  # noqa: PLC0415

            buf = _io.BytesIO()
            pq.write_table(table, buf)
            sha = hashlib.sha256(buf.getvalue()).hexdigest()
        db = self.stores.workspace(workspace_id)

        def tx(c):
            found = c.execute("SELECT current_version FROM datasets WHERE name = ?", [name]).fetchall()
            current = found[0] if found else None
            version = (current[0] + 1) if current else 1
            prev_sha, prev_physical = None, None
            if current:
                prev_sha, prev_physical = c.execute(
                    "SELECT source_sha256, physical_table FROM dataset_versions WHERE dataset = ? AND version = ?",
                    [name, current[0]]).fetchall()[0]
            if prev_sha == sha and not append_to_previous:
                return {"dataset": name, "version": current[0], "unchanged": True, "rows": table.num_rows}
            physical = f"{name}__v{version}"
            c.register("_incoming", table)
            if append_to_previous and prev_physical:
                c.execute(f'CREATE TABLE data."{physical}" AS SELECT * FROM data."{prev_physical}" UNION ALL BY NAME SELECT * FROM _incoming')
            else:
                c.execute(f'CREATE TABLE data."{physical}" AS SELECT * FROM _incoming')
            c.unregister("_incoming")
            rows = c.execute(f'SELECT count(*) FROM data."{physical}"').fetchall()[0][0]
            frame = c.execute(f'SELECT * FROM data."{physical}" LIMIT 200000').df()
            prof_json = json.loads(json.dumps(profile_frame(frame).to_dict(), default=str)) if len(frame) else {}
            schema = [{"name": r[0], "type": str(r[1]).lower()} for r in c.execute(f'DESCRIBE data."{physical}"').fetchall()]
            c.execute(
                "INSERT INTO dataset_versions (dataset, version, physical_table, source, source_sha256, "
                "row_count, schema, profile, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [name, version, physical, source, sha, rows, json.dumps(schema), json.dumps(prof_json, default=str), actor.user_id],
            )
            if current:
                c.execute("UPDATE datasets SET current_version = ?, updated_at = now() WHERE name = ?", [version, name])
            else:
                c.execute(
                    "INSERT INTO datasets (name, title, description, owner, classification, current_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [name, title or name.replace("_", " ").title(), description, actor.user_id, classification, version],
                )
            return {"dataset": name, "version": version, "unchanged": False, "rows": rows}

        result = db.write(tx)
        self.guard.audit(actor, workspace_id, "data.ingest", name, "ok", {"version": result["version"], "rows": result["rows"],
                                                                         "source": source})
        return result

    def set_column_policy(self, actor: Principal, workspace_id: str, dataset: str, column: str,
                          sensitivity: str, min_role: str) -> None:
        self.guard.require(actor, workspace_id, "workspace.admin", f"{dataset}.{column}")
        if min_role not in RANK:
            raise ValueError("unknown role")
        self.stores.workspace(workspace_id).execute(
            "INSERT OR REPLACE INTO column_policies VALUES (?, ?, ?, ?)",
            [dataset, column.lower(), sensitivity, min_role],
        )
        self.guard.audit(actor, workspace_id, "data.column_policy", f"{dataset}.{column}", "ok",
                         {"min_role": min_role})

    # -- catalog ----------------------------------------------------------------------------

    def catalog(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "data.read")
        db = self.stores.workspace(workspace_id)
        rows = db.query(
            "SELECT d.name, d.title, d.description, d.classification, d.current_version AS version, "
            "v.row_count, v.schema, v.source, v.created_at AS refreshed_at "
            "FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version "
            "ORDER BY d.name"
        )
        for r in rows:
            r["schema"] = json.loads(r["schema"])
        return rows

    def dataset_profile(self, actor: Principal, workspace_id: str, name: str) -> dict[str, Any]:
        self.guard.require(actor, workspace_id, "data.read", name)
        row = self.stores.workspace(workspace_id).query(
            "SELECT v.profile FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
            "AND v.version = d.current_version WHERE d.name = ?",
            [name],
        )
        if not row:
            raise KeyError(name)
        return json.loads(row[0]["profile"] or "{}")

    def policy_for(self, actor: Principal, workspace_id: str) -> GuardPolicy:
        db = self.stores.workspace(workspace_id)
        tables = {
            r["name"]: r["physical_table"]
            for r in db.query(
                "SELECT d.name, v.physical_table FROM datasets d JOIN dataset_versions v "
                "ON v.dataset = d.name AND v.version = d.current_version"
            )
        }
        role = self.guard.role(actor, workspace_id) or "viewer"
        restricted: dict[str, set[str]] = {}
        for p in db.query("SELECT dataset, column_name, min_role FROM column_policies"):
            if RANK[role] < RANK[p["min_role"]]:
                restricted.setdefault(p["dataset"], set()).add(p["column_name"])
        # row policies: a policy naming the user wins over one naming their role; the system principal is never restricted
        rows: dict[str, dict[str, list]] = {}
        if actor.kind != "system":
            by_role: dict[tuple[str, str], list] = {}
            by_user: dict[tuple[str, str], list] = {}
            for p in db.query("SELECT dataset, column_name, principal_kind, principal, allowed_values FROM row_policies"):
                key = (p["dataset"], p["column_name"])
                if p["principal_kind"] == "user" and p["principal"] == actor.user_id:
                    by_user[key] = json.loads(p["allowed_values"])
                elif p["principal_kind"] == "role" and p["principal"] == role:
                    by_role[key] = json.loads(p["allowed_values"])
            for key, values in {**by_role, **by_user}.items():
                rows.setdefault(key[0], {})[key[1]] = values
        return GuardPolicy(tables=tables, restricted_columns=restricted, row_filters=rows)

    # -- row policies -----------------------------------------------------------------------

    def set_row_policy(self, actor: Principal, workspace_id: str, dataset: str, column: str, *, principal_kind: str,
                       principal: str, allowed_values: list) -> dict[str, Any]:
        """Members named by user id or role see only rows whose `column` is in `allowed_values`. Owners manage it."""
        self.guard.require(actor, workspace_id, "workspace.admin", dataset)
        if principal_kind not in ("user", "role"):
            raise ValueError("principal_kind must be user or role")
        if principal_kind == "role" and principal not in RANK:
            raise ValueError(f"unknown role {principal}")
        db = self.stores.workspace(workspace_id)
        if not db.scalar("SELECT count(*) FROM datasets WHERE name = ?", [dataset]):
            raise KeyError(dataset)
        db.execute("INSERT OR REPLACE INTO row_policies (dataset, column_name, principal_kind, principal, allowed_values, created_by) "
                   "VALUES (?, ?, ?, ?, ?, ?)", [dataset, column, principal_kind, principal, json.dumps(list(allowed_values)), actor.user_id])
        self.guard.audit(actor, workspace_id, "policy.rows", dataset, "ok",
                         {"column": column, principal_kind: principal, "values": len(allowed_values)})
        return {"dataset": dataset, "column": column, "principal_kind": principal_kind, "principal": principal, "allowed_values": list(allowed_values)}

    def row_policies(self, actor: Principal, workspace_id: str, dataset: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "workspace.admin")
        sql = "SELECT dataset, column_name AS column, principal_kind, principal, allowed_values, created_by, created_at FROM row_policies"
        rows = self.stores.workspace(workspace_id).query(sql + (" WHERE dataset = ?" if dataset else "") + " ORDER BY dataset, column_name",
                                                         [dataset] if dataset else [])
        for r in rows:
            r["allowed_values"] = json.loads(r["allowed_values"])
        return rows

    def delete_row_policy(self, actor: Principal, workspace_id: str, dataset: str, column: str, *, principal_kind: str, principal: str) -> bool:
        self.guard.require(actor, workspace_id, "workspace.admin", dataset)
        db = self.stores.workspace(workspace_id)
        n = db.scalar("SELECT count(*) FROM row_policies WHERE dataset = ? AND column_name = ? AND principal_kind = ? AND principal = ?",
                      [dataset, column, principal_kind, principal])
        db.execute("DELETE FROM row_policies WHERE dataset = ? AND column_name = ? AND principal_kind = ? AND principal = ?",
                   [dataset, column, principal_kind, principal])
        self.guard.audit(actor, workspace_id, "policy.rows.delete", dataset, "ok", {"column": column, principal_kind: principal})
        return bool(n)

    # -- queries ----------------------------------------------------------------------------

    def query(self, actor: Principal, workspace_id: str, sql: str, *, task_id: str | None = None,
              max_rows: int = 10_000) -> QueryResult:
        self.guard.require(actor, workspace_id, "data.query")
        db = self.stores.workspace(workspace_id)
        policy = self.policy_for(actor, workspace_id)
        policy.max_rows = max_rows
        try:
            guarded = check_read_query(sql, policy)
        except QueryRejected as exc:
            self._history(db, actor, task_id, sql, None, 0, "rejected", str(exc))
            self.guard.audit(actor, workspace_id, "data.query", None, "rejected", {"reason": str(exc)})
            raise
        start = time.perf_counter()
        try:
            with db.read() as cur:
                rel = cur.execute(guarded.sql)
                cols = [d[0] for d in rel.description]
                rows = [list(r) for r in rel.fetchall()]
        except Exception as exc:
            ms = int((time.perf_counter() - start) * 1000)
            self._history(db, actor, task_id, sql, None, ms, "error", str(exc)[:500])
            raise
        ms = int((time.perf_counter() - start) * 1000)
        qid = self._history(db, actor, task_id, sql, len(rows), ms, "ok", None)
        return QueryResult(cols, rows, sql, guarded.sql, guarded.datasets, ms, qid)

    @staticmethod
    def _history(db, actor, task_id, sql, rows, ms, outcome, error) -> int:
        def tx(c):
            qid = c.execute("SELECT nextval('query_seq')").fetchone()[0]
            c.execute(
                "INSERT INTO query_history (id, actor, task_id, sql, rows, ms, outcome, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [qid, actor.user_id, task_id, sql, rows, ms, outcome, error],
            )
            return qid

        return db.write(tx)

    def history(self, actor: Principal, workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "data.read")
        return self.stores.workspace(workspace_id).query(
            "SELECT id, occurred_at, actor, task_id, sql, rows, ms, outcome, error FROM query_history "
            "ORDER BY id DESC LIMIT ?",
            [limit],
        )


def _read_table(content: bytes, filename: str) -> pa.Table:
    lower = filename.lower()
    if lower.endswith(".parquet"):
        return pq.read_table(io.BytesIO(content))
    if lower.endswith((".csv", ".txt", ".tsv")):
        opts = pacsv.ParseOptions(delimiter="\t" if lower.endswith(".tsv") else ",")
        return pacsv.read_csv(io.BytesIO(content), parse_options=opts)
    raise ValueError("supported formats: CSV, TSV, Parquet")


__all__ = ["DataService", "QueryRejected", "QueryResult", "normalise_name"]
