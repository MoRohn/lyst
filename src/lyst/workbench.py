# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The analyst's workbench: guarded SQL and sandboxed Python over governed data, every run recorded and reproducible.

SQL runs through the same read-only guard as everything else. Python runs in the OS sandbox (``lyst.sandbox``)
on inputs the caller names as SQL queries; those queries run first, under the caller's column and row policies,
so the code can only ever see what its author may see. Each run records the code, the inputs (SQL, dataset versions,
row counts, a digest of the rows), the environment fingerprint, the outcome and a digest of the result.

``reproduce`` runs the same code again on the same queries and answers in the scenario shape:
``{same, differences, observed_changed, environment}``. ``save`` turns a table result into a dataset version whose
source names the run, with lineage to the datasets it read.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import pandas as pd
import pyarrow as pa

from . import environment, sandbox
from .security import PERMISSIONS, Guard, Principal
from .store import Stores

PERMISSIONS.setdefault("workbench.python", "editor")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
MAX_INPUT_ROWS = 200_000
MAX_CODE = 100_000


def frame_digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(("|".join(map(str, frame.columns)) + "\n" + frame.to_csv(index=False)).encode()).hexdigest()[:16]


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _json_rows(frame: pd.DataFrame, n: int) -> list[list[Any]]:
    return json.loads(frame.head(n).to_json(orient="values", date_format="iso"))


class WorkbenchService:
    def __init__(self, stores: Stores, guard: Guard, data, knowledge, home) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.knowledge = knowledge
        self.home = home
        self.limits = sandbox.Limits()

    def status(self) -> dict[str, Any]:
        kind, why = sandbox.backend()
        return {"python": {"available": kind is not None, "backend": kind, "detail": why,
                           "limits": {"timeout_seconds": self.limits.timeout, "cpu_seconds": self.limits.cpu_seconds,
                                      "memory_mb": self.limits.memory_mb, "max_input_rows": MAX_INPUT_ROWS}},
                "sql": {"available": True, "detail": "read-only, guarded, column and row policies applied"},
                "environment": environment.fingerprint()}

    def sql(self, actor: Principal, ws: str, sql: str, *, max_rows: int = 1000) -> dict[str, Any]:
        r = self.data.query(actor, ws, sql, max_rows=min(max_rows, 10_000))
        return {"columns": r.columns, "rows": json.loads(r.frame().to_json(orient="values", date_format="iso")),
                "executed_sql": r.executed_sql, "ms": r.ms, "query_id": r.query_id, "datasets": r.datasets}

    def _inputs(self, actor: Principal, ws: str, inputs: dict[str, str]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
        frames, record = {}, {}
        versions = {d["name"]: d["version"] for d in self.data.catalog(actor, ws)}
        for name, sql in inputs.items():
            if not _IDENT.match(name) or name in ("pd", "np", "data", "result"):
                raise ValueError(f"input name {name!r} must be a lowercase identifier (and not pd, np, data or result)")
            r = self.data.query(actor, ws, sql, max_rows=MAX_INPUT_ROWS)
            frame = r.frame()
            frames[name] = frame
            record[name] = {"sql": sql, "datasets": {d: versions.get(d) for d in r.datasets}, "rows": len(frame),
                            "columns": list(frame.columns), "digest": frame_digest(frame),
                            "truncated": len(frame) >= MAX_INPUT_ROWS}
        return frames, record

    def python(self, actor: Principal, ws: str, code: str, *, inputs: dict[str, str] | None = None,
               timeout: float | None = None, reproduces: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "workbench.python")
        if len(code) > MAX_CODE:
            raise ValueError("the code is longer than 100,000 characters")
        kind, why = sandbox.backend()
        if kind is None:
            raise ValueError(why)
        frames, record = self._inputs(actor, ws, inputs or {})
        limits = sandbox.Limits(timeout=min(timeout or self.limits.timeout, 300), cpu_seconds=self.limits.cpu_seconds,
                                memory_mb=self.limits.memory_mb)
        out = sandbox.run(code, frames, limits=limits, hidden=[str(self.home)])
        result: dict[str, Any] | None = None
        rdigest = None
        if out.status == "ok" and out.result_kind == "table" and out.table is not None:
            result = {"columns": [str(c) for c in out.table.columns], "rows": len(out.table), "preview": _json_rows(out.table, 50)}
            rdigest = frame_digest(out.table)
        elif out.status == "ok" and out.result_kind == "value":
            result = {"value": out.result}
            rdigest = _digest(out.result)
        env = environment.fingerprint()
        rid = "wb_" + uuid.uuid4().hex[:10]
        input_digest = _digest({"code": code, "inputs": {k: v["digest"] for k, v in record.items()}})
        self.stores.workspace(ws).execute(
            "INSERT INTO workbench_runs (id, kind, code, inputs, environment, backend, status, error, stdout, result_kind, result, "
            "input_digest, result_digest, seconds, reproduces, created_by) VALUES (?, 'python', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [rid, code, json.dumps(record), json.dumps(env), out.backend, out.status, out.error, (out.stdout or "")[:20000],
             out.result_kind, json.dumps(result, default=str) if result is not None else None, input_digest, rdigest,
             out.seconds, reproduces, actor.user_id])
        self.guard.audit(actor, ws, "workbench.python", rid, out.status if out.status != "ok" else "ok",
                         {"inputs": list(record), "seconds": out.seconds, "backend": out.backend})
        payload = self.get(actor, ws, rid)
        if out.table is not None:
            self._tables[rid] = out.table  # kept briefly so "save as dataset" right after a run needs no rerun
            while len(self._tables) > 8:
                self._tables.pop(next(iter(self._tables)))
        return payload

    _tables: dict[str, pd.DataFrame] = {}

    def get(self, actor: Principal, ws: str, rid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read", rid)
        rows = self.stores.workspace(ws).query("SELECT * FROM workbench_runs WHERE id = ?", [rid])
        if not rows:
            raise KeyError(rid)
        r = rows[0]
        for k in ("inputs", "environment", "result"):
            r[k] = json.loads(r[k]) if r[k] else None
        if r["created_by"] != actor.user_id:
            self.guard.require(actor, ws, "audit.read", rid)  # someone else's code and output: admins only
        return r

    def list(self, actor: Principal, ws: str, limit: int = 50) -> list[dict[str, Any]]:
        role = self.guard.require(actor, ws, "data.read")
        sql = ("SELECT id, kind, status, result_kind, seconds, backend, reproduces, saved_as, created_by, created_at, "
               "substr(code, 1, 200) AS code_start FROM workbench_runs")
        params: list[Any] = []
        if role not in ("admin", "owner"):
            sql += " WHERE created_by = ?"
            params.append(actor.user_id)
        return self.stores.workspace(ws).query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, min(limit, 500)])

    def reproduce(self, actor: Principal, ws: str, rid: str) -> dict[str, Any]:
        was = self.get(actor, ws, rid)
        inputs = {k: v["sql"] for k, v in (was["inputs"] or {}).items()}
        now = self.python(actor, ws, was["code"], inputs=inputs, reproduces=rid)
        changed_inputs = [k for k, v in (was["inputs"] or {}).items() if now["inputs"][k]["digest"] != v["digest"]]
        differences = []
        if now["status"] != was["status"]:
            differences.append({"what": "status", "was": was["status"], "now": now["status"]})
        if now["result_digest"] != was["result_digest"]:
            differences.append({"what": "result", "was": was["result_digest"], "now": now["result_digest"]})
        out = {"run": rid, "reproduction": now["id"], "same": not differences, "differences": differences,
               "observed_changed": bool(changed_inputs), "changed_inputs": changed_inputs,
               "environment": environment.diff(was["environment"], now["environment"])}
        self.guard.audit(actor, ws, "workbench.reproduce", rid, "ok", {"same": out["same"], "observed_changed": out["observed_changed"]})
        return out

    def save(self, actor: Principal, ws: str, rid: str, name: str, *, description: str | None = None) -> dict[str, Any]:
        """A table result as a dataset version, its source naming the run and its lineage the datasets it read."""
        run = self.get(actor, ws, rid)
        if run["result_kind"] != "table":
            raise ValueError("only a table result (a DataFrame assigned to result) can be saved as a dataset")
        table = self._tables.get(rid)
        if table is None:
            again = self.python(actor, ws, run["code"], inputs={k: v["sql"] for k, v in run["inputs"].items()}, reproduces=rid)
            if again["result_digest"] != run["result_digest"]:
                raise ValueError("the inputs changed since this run, so its result can no longer be rebuilt exactly; "
                                 "run the code again and save that run")
            table = self._tables[again["id"]]
        r = self.data.ingest_arrow(actor, ws, name, pa.Table.from_pandas(table, preserve_index=False), source=f"workbench:{rid}",
                                   description=description or f"Computed in the workbench by run {rid}.")
        read = sorted({d for v in run["inputs"].values() for d in v["datasets"]})
        for d in read:
            self.knowledge.relate(actor, ws, "dataset", r["dataset"], "derived_from", "dataset", d,
                                  evidence={"workbench_run": rid, "version": run["inputs"]})
        self.stores.workspace(ws).execute("UPDATE workbench_runs SET saved_as = ? WHERE id = ?", [r["dataset"], rid])
        return {**r, "derived_from": read}


__all__ = ["WorkbenchService", "frame_digest"]
