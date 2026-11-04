# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Versioned data contracts: what a dataset promises the metrics, workflows and dashboards built on it.

A contract lists the columns and their types, which are required, the key that identifies a row, accepted values
for categorical columns, value ranges, references to other datasets' keys, a freshness expectation and an owner.
``draft`` proposes one from the current version and its insights (the person edits, then saves it as version 1).

Every new dataset version is checked against the active contract as soon as it lands (the ingest hook), and a
check can be run by hand. Results are classified:

* **breaking**: a column the contract names is gone or changed type. Definitions that read it no longer compute.
* **fail**: the data breaks a promise (nulls in a required column, duplicate keys, a value outside the accepted
  set or range, an orphan reference).
* **warn**: the data is older than the freshness expectation, or a new column appeared.

A breaking or failing check lists the downstream assets affected (from the catalog's impact walk), raises a finding
in the inbox of the contract owner and the workspace's owners and admins, and is kept in the check history. The data
itself is never rejected: versions are immutable and every earlier version stays, so the person decides.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .catalog import _family, _q, impact_of
from .security import Guard, Principal
from .store import Stores

log = logging.getLogger("lyst.contracts")


class ColumnRule(BaseModel):
    name: str
    type: str
    required: bool = False
    accepted_values: list[str] | None = None
    min: float | None = None
    max: float | None = None


class Reference(BaseModel):
    column: str
    dataset: str
    to_column: str


class Freshness(BaseModel):
    column: str
    max_age_days: int = Field(ge=1)


class ContractSpec(BaseModel):
    columns: list[ColumnRule]
    key: list[str] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    freshness: Freshness | None = None
    owner: str | None = None
    allow_new_columns: bool = True

    @field_validator("columns")
    @classmethod
    def _unique(cls, v: list[ColumnRule]) -> list[ColumnRule]:
        names = [c.name for c in v]
        if len(names) != len(set(names)):
            raise ValueError("a column appears twice in the contract")
        return v


class ContractService:
    def __init__(self, stores: Stores, guard: Guard, catalog, semantic) -> None:
        self.stores = stores
        self.guard = guard
        self.catalog = catalog
        self.semantic = semantic

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- definition -----------------------------------------------------------------------------

    def draft(self, actor: Principal, ws: str, dataset: str) -> dict[str, Any]:
        """A contract proposed from what the current version is: its schema, its key, columns that are never empty,
        the values of small categorical columns, and references discovery found. Nothing is saved."""
        self.guard.require(actor, ws, "data.read", dataset)
        ins = self.catalog.insights(actor, ws, dataset)
        cols = []
        for c in ins["columns"]:
            rule: dict[str, Any] = {"name": c["name"], "type": c["type"], "required": (c.get("nulls") or 0) == 0}
            if c["role"] in ("dimension", "flag") and c.get("top") and 0 < (c.get("distinct") or 0) <= 20 and len(c["top"]) >= (c.get("distinct") or 0):
                rule["accepted_values"] = sorted(str(v) for v, _ in c["top"])
            if c.get("semantic_type") in ("amount", "count") and c.get("robust") and not any(r["kind"] == "negative_amount" and c["name"] in r["columns"]
                                                                                           for r in ins.get("risks", [])):
                rule["min"] = 0.0
            cols.append(rule)
        refs = [{"column": r["from_column"], "dataset": r["to_dataset"], "to_column": r["to_column"]}
                for r in ins.get("relationships", []) if r["kind"] == "foreign_key" and r["from_dataset"] == dataset]
        spec = ContractSpec(columns=[ColumnRule(**c) for c in cols], key=ins.get("grain") or [], references=[Reference(**r) for r in refs],
                            owner=self._db(ws).scalar("SELECT owner FROM datasets WHERE name = ?", [dataset]))
        return spec.model_dump()

    def define(self, actor: Principal, ws: str, dataset: str, spec: ContractSpec, *, note: str = "") -> dict[str, Any]:
        """Save a new contract version (admins) and check the current data against it."""
        self.guard.require(actor, ws, "contract.define", dataset)
        db = self._db(ws)
        if not db.scalar("SELECT count(*) FROM datasets WHERE name = ?", [dataset]):
            raise KeyError(dataset)
        names = {c.name for c in spec.columns}
        for k in spec.key:
            if k not in names:
                raise ValueError(f"key column {k} is not in the contract's columns")
        for r in spec.references:
            if r.column not in names:
                raise ValueError(f"reference column {r.column} is not in the contract's columns")
            if not db.scalar("SELECT count(*) FROM datasets WHERE name = ?", [r.dataset]):
                raise ValueError(f"referenced dataset {r.dataset} does not exist")
        if spec.freshness and spec.freshness.column not in names:
            raise ValueError(f"freshness column {spec.freshness.column} is not in the contract's columns")

        def tx(c):
            v = (c.execute("SELECT max(version) FROM data_contracts WHERE dataset = ?", [dataset]).fetchone()[0] or 0) + 1
            c.execute("UPDATE data_contracts SET status = 'superseded' WHERE dataset = ? AND status = 'active'", [dataset])
            c.execute("INSERT INTO data_contracts (dataset, version, spec, status, note, created_by) VALUES (?, ?, ?, 'active', ?, ?)",
                      [dataset, v, spec.model_dump_json(), note, actor.user_id])
            return v

        version = db.write(tx)
        self.guard.audit(actor, ws, "contract.define", dataset, "ok", {"version": version})
        check = self.check(actor, ws, dataset)
        return {"dataset": dataset, "version": version, "check": check}

    def retire(self, actor: Principal, ws: str, dataset: str) -> None:
        self.guard.require(actor, ws, "contract.define", dataset)
        self._db(ws).execute("UPDATE data_contracts SET status = 'retired' WHERE dataset = ? AND status = 'active'", [dataset])
        self.guard.audit(actor, ws, "contract.retire", dataset, "ok")

    def _active(self, ws: str, dataset: str) -> tuple[int, ContractSpec] | None:
        r = self._db(ws).query("SELECT version, spec FROM data_contracts WHERE dataset = ? AND status = 'active'", [dataset])
        return (r[0]["version"], ContractSpec.model_validate_json(r[0]["spec"])) if r else None

    def get(self, actor: Principal, ws: str, dataset: str) -> dict[str, Any] | None:
        self.guard.require(actor, ws, "data.read", dataset)
        rows = self._db(ws).query("SELECT version, spec, status, note, created_by, created_at FROM data_contracts WHERE dataset = ? "
                                  "ORDER BY version DESC", [dataset])
        if not rows:
            return None
        for r in rows:
            r["spec"] = json.loads(r["spec"])
        active = next((r for r in rows if r["status"] == "active"), None)
        return {"dataset": dataset, "active": active, "versions": rows, "status": self.status(ws, dataset)}

    def status(self, ws: str, dataset: str) -> dict[str, Any] | None:
        """The active contract version and its latest check, for catalog rows and lineage."""
        active = self._active(ws, dataset)
        if not active:
            return None
        r = self._db(ws).query("SELECT outcome, dataset_version, checked_at, results FROM contract_checks WHERE dataset = ? AND contract_version = ? "
                               "ORDER BY checked_at DESC LIMIT 1", [dataset, active[0]])
        if not r:
            return {"version": active[0], "outcome": None}
        results = json.loads(r[0]["results"])
        return {"version": active[0], "outcome": r[0]["outcome"], "dataset_version": r[0]["dataset_version"], "checked_at": r[0]["checked_at"],
                "problems": [x["text"] for x in results if x["severity"] in ("breaking", "fail")][:5]}

    # -- checking -------------------------------------------------------------------------------

    def check(self, actor: Principal, ws: str, dataset: str, version: int | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read", dataset)
        return self._check(actor, ws, dataset, version)

    def after_ingest(self, actor: Principal, ws: str, result: dict[str, Any]) -> None:
        """Ingestion hook: check the new version against the active contract. Never fails the ingest."""
        if result.get("unchanged") or not self._active(ws, result["dataset"]):
            return
        try:
            self._check(actor, ws, result["dataset"], result["version"])
        except Exception:  # noqa: BLE001 - the version is committed; a failed check is logged, not raised
            log.exception("contract check for %s v%s failed", result.get("dataset"), result.get("version"))

    def _check(self, actor: Principal, ws: str, dataset: str, version: int | None) -> dict[str, Any]:
        active = self._active(ws, dataset)
        if not active:
            raise KeyError(f"{dataset} has no active contract")
        cversion, spec = active
        db = self._db(ws)
        version = version or db.scalar("SELECT current_version FROM datasets WHERE name = ?", [dataset])
        row = db.query("SELECT physical_table, schema FROM dataset_versions WHERE dataset = ? AND version = ?", [dataset, version])
        if not row:
            raise KeyError(f"{dataset} v{version}")
        table = f'data.{_q(row[0]["physical_table"])}'
        schema = {c["name"]: c["type"] for c in json.loads(row[0]["schema"])}
        results: list[dict[str, Any]] = []
        broken_cols: set[str] = set()

        def add(severity: str, kind: str, cols: list[str], text: str, **extra) -> None:
            results.append({"severity": severity, "kind": kind, "columns": cols, "text": text, **extra})
            if severity in ("breaking", "fail"):
                broken_cols.update(cols)

        with db.read() as cur:
            for rule in spec.columns:
                if rule.name not in schema:
                    add("breaking", "column_removed", [rule.name], f"column {rule.name} is missing")
                    continue
                if _family(schema[rule.name]) != _family(rule.type):
                    add("breaking", "type_changed", [rule.name], f"{rule.name} changed type from {rule.type} to {schema[rule.name]}")
                    continue
                col = _q(rule.name)
                if rule.required:
                    n = cur.execute(f"SELECT count(*) FROM {table} WHERE {col} IS NULL").fetchone()[0]
                    if n:
                        add("fail", "required_missing", [rule.name], f"{n:,} rows have no {rule.name}, which is required", rows=n)
                if rule.accepted_values is not None:
                    allowed = ", ".join("'" + v.replace("'", "''") + "'" for v in rule.accepted_values) or "NULL"
                    bad = cur.execute(f"SELECT CAST({col} AS VARCHAR) AS v, count(*) FROM {table} WHERE {col} IS NOT NULL AND "
                                      f"CAST({col} AS VARCHAR) NOT IN ({allowed}) GROUP BY 1 ORDER BY 2 DESC LIMIT 10").fetchall()
                    if bad:
                        add("fail", "unexpected_values", [rule.name],
                            f"{rule.name} has values outside the accepted set: {', '.join(str(v) for v, _ in bad[:5])}",
                            values=[v for v, _ in bad])
                if (rule.min is not None or rule.max is not None) and _family(schema[rule.name]) == "number":
                    conds = ([f"{col} < {float(rule.min)!r}"] if rule.min is not None else []) + \
                            ([f"{col} > {float(rule.max)!r}"] if rule.max is not None else [])
                    n = cur.execute(f"SELECT count(*) FROM {table} WHERE {' OR '.join(conds)}").fetchone()[0]
                    if n:
                        rng = " to ".join(str(x) for x in (rule.min, rule.max) if x is not None)
                        add("fail", "out_of_range", [rule.name], f"{n:,} values of {rule.name} fall outside {rng}", rows=n)
            if spec.key and all(k in schema for k in spec.key):
                cols = ", ".join(_q(k) for k in spec.key)
                dup = cur.execute(f"SELECT count(*) - count(DISTINCT ({cols})) FROM {table}").fetchone()[0]
                if dup:
                    add("fail", "duplicate_key", list(spec.key), f"{dup:,} rows repeat the key {' × '.join(spec.key)}", rows=dup)
            for ref in spec.references:
                if ref.column not in schema:
                    continue
                target = db.query("SELECT v.physical_table FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
                                  "AND v.version = d.current_version WHERE d.name = ?", [ref.dataset])
                if not target:
                    add("fail", "reference_missing", [ref.column], f"{ref.dataset}, referenced by {ref.column}, no longer exists")
                    continue
                orphans = cur.execute(f"SELECT count(DISTINCT {_q(ref.column)}) FROM {table} WHERE {_q(ref.column)} IS NOT NULL AND "
                                      f"CAST({_q(ref.column)} AS VARCHAR) NOT IN (SELECT CAST({_q(ref.to_column)} AS VARCHAR) "
                                      f"FROM data.{_q(target[0]['physical_table'])} WHERE {_q(ref.to_column)} IS NOT NULL)").fetchone()[0]
                if orphans:
                    add("fail", "orphan_reference", [ref.column],
                        f"{orphans:,} values of {ref.column} have no match in {ref.dataset}.{ref.to_column}", values=orphans)
            if spec.freshness and spec.freshness.column in schema:
                latest = cur.execute(f"SELECT max(CAST({_q(spec.freshness.column)} AS DATE)) FROM {table}").fetchone()[0]
                if latest is not None:
                    age = (date.today() - latest).days
                    if age > spec.freshness.max_age_days:
                        add("warn", "stale", [spec.freshness.column],
                            f"the latest {spec.freshness.column} is {latest} ({age} days old; the contract expects at most {spec.freshness.max_age_days})")
        named = {c.name for c in spec.columns}
        new_cols = sorted(set(schema) - named)
        if new_cols:
            add("warn" if spec.allow_new_columns else "fail", "new_columns", new_cols, f"new column{'s' if len(new_cols) != 1 else ''}: {', '.join(new_cols)}")
        rank = {"breaking": 0, "fail": 1, "warn": 2}
        results.sort(key=lambda r: rank[r["severity"]])
        outcome = next((s for s in ("breaking", "fail", "warn") if any(r["severity"] == s for r in results)), "pass")
        impacted = impact_of(db, self.semantic, actor, ws, dataset, sorted(broken_cols)) if outcome in ("breaking", "fail") else []
        if outcome in ("breaking", "fail") and any(r["kind"] in ("duplicate_key", "orphan_reference") for r in results):
            # a broken key inflates every sum on the table, not just the key's own columns
            impacted = impact_of(db, self.semantic, actor, ws, dataset, None)
        cid = "cc_" + uuid.uuid4().hex[:12]
        db.execute("INSERT INTO contract_checks (id, dataset, contract_version, dataset_version, outcome, results, impacted, checked_by) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [cid, dataset, cversion, version, outcome, json.dumps(results, default=str), json.dumps(impacted, default=str), actor.user_id])
        self.guard.audit(actor, ws, "contract.check", dataset, outcome, {"contract_version": cversion, "dataset_version": version,
                                                                         "problems": len([r for r in results if r["severity"] in ("breaking", "fail")])})
        if outcome in ("breaking", "fail"):
            self._notify(ws, dataset, version, cid, outcome, results, impacted, spec.owner)
        return {"id": cid, "dataset": dataset, "contract_version": cversion, "dataset_version": version, "outcome": outcome,
                "results": results, "impacted": impacted}

    def checks(self, actor: Principal, ws: str, dataset: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read", dataset)
        rows = self._db(ws).query("SELECT * FROM contract_checks WHERE dataset = ? ORDER BY checked_at DESC LIMIT ?", [dataset, limit])
        for r in rows:
            r["results"], r["impacted"] = json.loads(r["results"]), json.loads(r["impacted"])
        return rows

    def _notify(self, ws: str, dataset: str, version: int, check_id: str, outcome: str, results: list[dict[str, Any]],
                impacted: list[dict[str, Any]], owner: str | None) -> None:
        """A finding in the inbox, deduplicated per dataset version, for the owner and the workspace's owners and admins."""
        db = self._db(ws)
        problems = [r["text"] for r in results if r["severity"] in ("breaking", "fail")]
        order = {"dashboard": 0, "workflow": 1, "monitor": 2, "metric": 3}
        assets = [f"{i['kind']} {i.get('title') or i['name']}" for i in sorted(impacted, key=lambda i: order.get(i["kind"], 9))
                  if i["kind"] in order]
        summary = (f"{dataset} v{version} {'breaks' if outcome == 'breaking' else 'fails'} its contract: {problems[0]}"
                   + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")
                   + (f". Affected: {', '.join(assets[:6])}" if assets else ". Nothing downstream reads the affected columns") + ".")
        key = f"contract:{dataset}:{version}"
        if db.scalar("SELECT count(*) FROM findings WHERE dedupe_key = ?", [key]):
            return
        fid = "f_" + uuid.uuid4().hex[:10]
        metric = next((i["name"] for i in impacted if i["kind"] == "metric"), "")
        db.execute("INSERT INTO findings (id, monitor_id, kind, period, label, metric, summary, priority, direction, z, value, baseline, task_id, "
                   "status, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'down', NULL, NULL, NULL, NULL, 'new', ?)",
                   [fid, f"contract:{dataset}", "contract_breaking" if outcome == "breaking" else "contract_failed", str(version),
                    f"{dataset} v{version}", metric, summary, 9.0 if outcome == "breaking" else 6.0 + min(3.0, len(assets) * 0.5), key])
        rows = self.stores.control.query("SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin')", [ws])
        for uid in {r["user_id"] for r in rows} | ({owner} if owner else set()):
            db.execute("INSERT INTO notifications (id, user_id, finding_id, channel, status) VALUES (?, ?, ?, 'inbox', 'unread')",
                       ["n_" + uuid.uuid4().hex[:10], uid, fid])
        log.info("contract %s on %s v%s (check %s)", outcome, dataset, version, check_id)


__all__ = ["ColumnRule", "ContractService", "ContractSpec", "Freshness", "Reference"]
