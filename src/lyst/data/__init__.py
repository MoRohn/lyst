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
        self.guard.require(actor, workspace_id, "data.ingest", name)
        name = normalise_name(name)
        if not _NAME.match(name):
            raise ValueError("invalid dataset name")
        table = _read_table(content, filename)
        table = table.rename_columns([normalise_name(c) for c in table.column_names])
        sha = hashlib.sha256(content).hexdigest()
        frame = table.to_pandas()
        prof = profile_frame(frame)
        prof_json = json.loads(json.dumps(prof.to_dict(), default=str))
        schema = [{"name": f.name, "type": str(f.type)} for f in table.schema]
        db = self.stores.workspace(workspace_id)

        def tx(c):
            current = c.execute("SELECT current_version FROM datasets WHERE name = ?", [name]).fetchone()
            version = (current[0] + 1) if current else 1
            prev_sha = None
            if current:
                prev_sha = c.execute(
                    "SELECT source_sha256 FROM dataset_versions WHERE dataset = ? AND version = ?",
                    [name, current[0]],
                ).fetchone()[0]
            if prev_sha == sha:
                return {"dataset": name, "version": current[0], "unchanged": True, "rows": table.num_rows}
            physical = f"{name}__v{version}"
            c.register("_incoming", table)
            c.execute(f'CREATE TABLE data."{physical}" AS SELECT * FROM _incoming')
            c.unregister("_incoming")
            c.execute(
                "INSERT INTO dataset_versions (dataset, version, physical_table, source, source_sha256, "
                "row_count, schema, profile, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [name, version, physical, filename, sha, table.num_rows, json.dumps(schema),
                 json.dumps(prof_json, default=str), actor.user_id],
            )
            if current:
                c.execute(
                    "UPDATE datasets SET current_version = ?, updated_at = now() WHERE name = ?",
                    [version, name],
                )
            else:
                c.execute(
                    "INSERT INTO datasets (name, title, description, owner, classification, current_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [name, title or name.replace("_", " ").title(), description, actor.user_id,
                     classification, version],
                )
            return {"dataset": name, "version": version, "unchanged": False, "rows": table.num_rows}

        result = db.write(tx)
        self.guard.audit(actor, workspace_id, "data.ingest", name, "ok", {"version": result["version"],
                                                                         "rows": result["rows"]})
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
        return GuardPolicy(tables=tables, restricted_columns=restricted)

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
