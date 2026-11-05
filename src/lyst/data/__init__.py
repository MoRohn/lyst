# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Governed datasets: versioned ingestion, catalog, profiling and guarded queries."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .. import observability as obs
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


def unique_names(names: list[str]) -> list[str]:
    """Normalised column names with collisions made distinct: "Total Sales" and "total-sales" both normalise to
    total_sales, so the second becomes total_sales_2 rather than two columns with one name."""
    out: list[str] = []
    seen: set[str] = set()
    for n in (normalise_name(c) for c in names):
        name, i = n, 2
        while name in seen:
            suffix = f"_{i}"
            name, i = n[:63 - len(suffix)] + suffix, i + 1
        seen.add(name)
        out.append(name)
    return out


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


def schema_diff(before: list[dict[str, str]], after: list[dict[str, str]]) -> dict[str, list[str]]:
    """Columns added, removed and retyped between two dataset schemas (``[{"name", "type"}]``)."""
    b, a = {c["name"]: c["type"] for c in before}, {c["name"]: c["type"] for c in after}
    return {"added": [n for n in a if n not in b], "removed": [n for n in b if n not in a],
            "retyped": [f"{n}:{b[n]}->{a[n]}" for n in a if n in b and a[n] != b[n]]}


class DataService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard
        # called with (actor, workspace_id, result) after a new version commits: insights, contract checks
        self.after_ingest: list = []
        self.events = None  # the event service (lane.attach): a schema change is staged with the version it describes
        # called with (actor, workspace_id, datasets) before a query runs; raise to refuse (governance.check_read)
        self.access_checks: list = []

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
        table: pa.Table | Any,
        *,
        source: str,
        sha: str | None = None,
        title: str | None = None,
        description: str | None = None,
        classification: str = "internal",
        append_to_previous: bool = False,
    ) -> dict[str, Any]:
        """A new dataset version from an Arrow table. With ``append_to_previous`` the new version is the current
        version's rows plus these (an incremental sync); otherwise it replaces them.

        ``table`` may also be a ``pyarrow.dataset.Dataset`` with normalised column names (a connector sync spooled to
        disk); DuckDB scans it lazily, so the rows are never all in memory at once. Its ``sha`` must be given."""
        name = normalise_name(name)  # authorised under the name it is written as
        self.guard.require(actor, workspace_id, "data.ingest", name)
        if not _NAME.match(name):
            raise ValueError("invalid dataset name")
        if isinstance(table, pa.Table):
            table = table.rename_columns(unique_names(table.column_names))
        elif sha is None or unique_names(table.schema.names) != table.schema.names:
            raise ValueError("a spooled dataset needs its sha256 and normalised column names")
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
            prev_sha, prev_physical, prev_schema, prev_rows = None, None, None, None
            if current:
                prev_sha, prev_physical, prev_schema, prev_rows = c.execute(
                    "SELECT source_sha256, physical_table, schema, row_count FROM dataset_versions WHERE dataset = ? AND version = ?",
                    [name, current[0]]).fetchall()[0]
            if prev_sha == sha and not append_to_previous:
                rows = table.num_rows if isinstance(table, pa.Table) else table.count_rows()
                return {"dataset": name, "version": current[0], "unchanged": True, "rows": rows}
            physical = f"{name}__v{version}"
            c.register("_incoming", table)
            if append_to_previous and prev_physical:
                c.execute(f'CREATE TABLE data."{physical}" AS SELECT * FROM data."{prev_physical}" UNION ALL BY NAME SELECT * FROM _incoming')
            else:
                c.execute(f'CREATE TABLE data."{physical}" AS SELECT * FROM _incoming')
            c.unregister("_incoming")
            rows = c.execute(f'SELECT count(*) FROM data."{physical}"').fetchall()[0][0]
            schema = [{"name": r[0], "type": str(r[1]).lower()} for r in c.execute(f'DESCRIBE data."{physical}"').fetchall()]
            c.execute(
                "INSERT INTO dataset_versions (dataset, version, physical_table, source, source_sha256, "
                "row_count, schema, profile, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [name, version, physical, source, sha, rows, json.dumps(schema), "{}", actor.user_id],
            )
            if current:
                c.execute("UPDATE datasets SET current_version = ?, updated_at = now() WHERE name = ?", [version, name])
            else:
                c.execute(
                    "INSERT INTO datasets (name, title, description, owner, classification, current_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [name, title or name.replace("_", " ").title(), description, actor.user_id, classification, version],
                )
            if current and self.events is not None:
                drift = schema_diff(json.loads(prev_schema or "[]"), schema)
                if drift["added"] or drift["removed"] or drift["retyped"]:
                    # staged in this transaction: the event exists exactly when the new version does
                    level = c.execute("SELECT classification FROM datasets WHERE name = ?", [name]).fetchone()[0]
                    self.events.stage(c, workspace_id, "dataset.schema_changed", subject=("dataset", name),
                                      actor=actor.user_id, dedupe_key=f"schema:{name}:v{version}",
                                      sensitivity=level if level in ("public", "internal", "confidential", "restricted") else "internal",
                                      data={"version": version, "previous_version": current[0], "rows": rows,
                                            "previous_rows": prev_rows, **{k: v[:20] for k, v in drift.items()}})
            return {"dataset": name, "version": version, "unchanged": False, "rows": rows, "physical": physical}

        result = db.write(tx)
        physical = result.pop("physical", None)
        if physical:
            # profiled after the commit, from a read connection: the single writer is not held while pandas works
            try:
                with db.read() as cur:
                    frame = cur.execute(f'SELECT * FROM data."{physical}" LIMIT 200000').df()
                if len(frame):
                    db.execute("UPDATE dataset_versions SET profile = ? WHERE dataset = ? AND version = ?",
                               [json.dumps(profile_frame(frame).to_dict(), default=str), name, result["version"]])
            except Exception:  # noqa: BLE001 - the version is committed; a profile is descriptive
                logging.getLogger("lyst.data").exception("profiling %s v%s failed", name, result["version"])
        self.guard.audit(actor, workspace_id, "data.ingest", name, "ok", {"version": result["version"], "rows": result["rows"],
                                                                         "source": source})
        for hook in self.after_ingest:
            try:
                hook(actor, workspace_id, result)
            except Exception:  # noqa: BLE001 - the version is committed; advisory work never undoes it
                logging.getLogger("lyst.data").exception("after-ingest hook failed for %s", name)
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
        prof = json.loads(row[0]["profile"] or "{}")
        hidden = self.policy_for(actor, workspace_id).restricted_columns.get(name, set())
        if hidden and prof:
            # the same column policy that masks a query masks the profile's samples, top values and statistics
            prof["columns"] = [c for c in prof.get("columns", []) if c.get("name") not in hidden]
            prof["correlations"] = [c for c in prof.get("correlations", []) if not _mentions(list(c[:2]), hidden)]
            prof["hierarchies"] = [h for h in prof.get("hierarchies", []) if not _mentions(h, hidden)]
            prof["suggest"] = {k: v for k, v in (prof.get("suggest") or {}).items() if not _mentions(v, hidden)}
        return prof

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
        above = [r for r, rank in RANK.items() if rank > RANK[role]]  # only the policies this caller is below
        for p in db.query("SELECT dataset, column_name FROM column_policies WHERE min_role IN "
                          f"({', '.join('?' for _ in above) or 'NULL'})", above):
            restricted.setdefault(p["dataset"], set()).add(p["column_name"])
        # row policies: a policy naming the user wins over one naming their role; the system principal is never restricted
        rows: dict[str, dict[str, list]] = {}
        if actor.kind != "system":
            by_role: dict[tuple[str, str], list] = {}
            by_user: dict[tuple[str, str], list] = {}
            for p in db.query("SELECT dataset, column_name, principal_kind, principal, allowed_values FROM row_policies "
                              "WHERE (principal_kind = 'user' AND principal = ?) OR (principal_kind = 'role' AND principal = ?)",
                              [actor.user_id, role]):
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
        schema = db.query("SELECT v.schema FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
                          "AND v.version = d.current_version WHERE d.name = ?", [dataset])
        if not schema:
            raise KeyError(dataset)
        column = normalise_name(column)  # the name ingestion gave the column, so the filter applies to it
        if column not in {c["name"] for c in json.loads(schema[0]["schema"])}:
            raise ValueError(f"{dataset} has no column {column}")
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
        if principal_kind not in ("user", "role"):
            raise ValueError("principal_kind must be user or role")
        db = self.stores.workspace(workspace_id)
        # a policy stored before names were normalised is still addressable by the name it was given
        names = sorted({column, normalise_name(column)})
        marks = ", ".join("?" for _ in names)
        args = [dataset, *names, principal_kind, principal]

        def tx(c):
            n = c.execute(f"SELECT count(*) FROM row_policies WHERE dataset = ? AND column_name IN ({marks}) AND principal_kind = ? "
                          "AND principal = ?", args).fetchone()[0]
            c.execute(f"DELETE FROM row_policies WHERE dataset = ? AND column_name IN ({marks}) AND principal_kind = ? AND principal = ?",
                      args)
            return n

        n = db.write(tx)
        self.guard.audit(actor, workspace_id, "policy.rows.delete", dataset, "ok" if n else "not_found",
                         {"column": column, principal_kind: principal})
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
        for check in self.access_checks:
            try:
                check(actor, workspace_id, guarded.datasets)
            except Exception as exc:
                self._history(db, actor, task_id, sql, None, 0, "rejected", str(exc)[:500], guarded.datasets)
                self.guard.audit(actor, workspace_id, "data.query", None, "rejected",
                                 {"reason": str(exc)[:300], "datasets": sorted(guarded.datasets)})
                raise
        start = time.perf_counter()
        try:
            with db.read() as cur:
                rel = cur.execute(guarded.sql)
                cols = [d[0] for d in rel.description]
                rows = [list(r) for r in rel.fetchall()]
        except Exception as exc:
            ms = int((time.perf_counter() - start) * 1000)
            self._history(db, actor, task_id, sql, None, ms, "error", str(exc)[:500], guarded.datasets)
            logging.getLogger("lyst.data").warning("query failed in %s after %d ms: %s", workspace_id, ms,
                                                   type(exc).__name__)
            raise
        ms = int((time.perf_counter() - start) * 1000)
        qid = self._history(db, actor, task_id, sql, len(rows), ms, "ok", None, guarded.datasets)
        return QueryResult(cols, rows, sql, guarded.sql, guarded.datasets, ms, qid)

    @staticmethod
    def _history(db, actor, task_id, sql, rows, ms, outcome, error, datasets=None) -> int:
        step = obs.step_seq.get() if task_id else None  # read here, on the caller's thread, not the writer's
        def tx(c):
            qid = c.execute("SELECT nextval('query_seq')").fetchone()[0]
            c.execute(
                "INSERT INTO query_history (id, actor, task_id, sql, rows, ms, outcome, error, step, datasets) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [qid, actor.user_id, task_id, sql, rows, ms, outcome, error, step, json.dumps(sorted(datasets)) if datasets else None],
            )
            return qid

        return db.write(tx)

    def history(self, actor: Principal, workspace_id: str, limit: int = 50, *, before_id: int | None = None) -> list[dict[str, Any]]:
        """Recent queries, newest first. Members see their own; workspace admins and owners see everyone's."""
        self.guard.require(actor, workspace_id, "data.read")
        everyone = RANK.get(self.guard.role(actor, workspace_id) or "viewer", 0) >= RANK["admin"] or actor.kind == "system"
        where, params = [], []
        if not everyone:
            where.append("actor = ?")
            params.append(actor.user_id)
        if before_id is not None:
            where.append("id < ?")
            params.append(int(before_id))
        return self.stores.workspace(workspace_id).query(
            "SELECT id, occurred_at, actor, task_id, sql, rows, ms, outcome, error FROM query_history "
            + (f"WHERE {' AND '.join(where)} " if where else "") + "ORDER BY id DESC LIMIT ?",
            [*params, max(1, min(int(limit), 500))],
        )


def _mentions(value: Any, names: set[str]) -> bool:
    if isinstance(value, str):
        return value in names
    if isinstance(value, list | tuple):
        return any(_mentions(v, names) for v in value)
    if isinstance(value, dict):
        return any(_mentions(v, names) for v in value.values())
    return False


def _read_table(content: bytes, filename: str) -> pa.Table:
    lower = filename.lower()
    if lower.endswith(".parquet"):
        return pq.read_table(io.BytesIO(content))
    if lower.endswith((".csv", ".txt", ".tsv")):
        opts = pacsv.ParseOptions(delimiter="\t" if lower.endswith(".tsv") else ",")
        return pacsv.read_csv(io.BytesIO(content), parse_options=opts)
    raise ValueError("supported formats: CSV, TSV, Parquet")


__all__ = ["DataService", "QueryRejected", "QueryResult", "normalise_name", "unique_names"]
