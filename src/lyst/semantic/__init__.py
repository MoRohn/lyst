# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Governed metrics and glossary.

A metric is defined once, versioned, and compiled to SQL by code, never written freshly by a
model. "Revenue last quarter" resolves to the approved ``revenue`` definition, and a term that
maps to two approved metrics is reported as ambiguous instead of guessed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..security import Guard, Principal
from ..store import Stores

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
Agg = Literal["sum", "count", "count_distinct", "avg", "min", "max"]
Op = Literal["=", "!=", ">", ">=", "<", "<=", "in", "not in"]


class Filter(BaseModel):
    column: str
    op: Op = "="
    value: Any

    @field_validator("column")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"invalid column name {v!r}")
        return v


class Measure(BaseModel):
    agg: Agg
    column: str | None = None
    filters: list[Filter] = Field(default_factory=list)

    @field_validator("column")
    @classmethod
    def _ident(cls, v: str | None) -> str | None:
        if v is not None and not _IDENT.match(v):
            raise ValueError(f"invalid column name {v!r}")
        return v


class MetricSpec(BaseModel):
    name: str
    title: str
    description: str = ""
    dataset: str
    kind: Literal["simple", "ratio"] = "simple"
    measure: Measure | None = None
    numerator: Measure | None = None
    denominator: Measure | None = None
    time_column: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    unit: str = ""
    format: Literal["number", "currency", "percent"] = "number"
    higher_is_better: bool = True
    synonyms: list[str] = Field(default_factory=list)

    @field_validator("name", "dataset")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"invalid identifier {v!r}")
        return v

    def columns(self) -> set[str]:
        cols: set[str] = set(self.dimensions)
        if self.time_column:
            cols.add(self.time_column)
        for m in (self.measure, self.numerator, self.denominator):
            if m:
                if m.column:
                    cols.add(m.column)
                cols.update(f.column for f in m.filters)
        cols.update(f.column for f in self.filters)
        return cols


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _cond(f: Filter) -> str:
    if f.op in ("in", "not in"):
        vals = f.value if isinstance(f.value, list) else [f.value]
        return f"{_q(f.column)} {f.op.upper()} ({', '.join(_lit(v) for v in vals)})"
    return f"{_q(f.column)} {f.op} {_lit(f.value)}"


def _agg(m: Measure) -> str:
    if m.agg not in ("count",) and not m.column:
        raise ValueError(f"{m.agg} needs a column")
    where = " AND ".join(_cond(f) for f in m.filters)
    col = _q(m.column) if m.column else None
    if where:
        target = f"CASE WHEN {where} THEN {col or 1} END"
    else:
        target = col or "*"
    if m.agg == "count_distinct":
        return f"COUNT(DISTINCT {target})"
    if m.agg == "count":
        return f"COUNT({target})"
    return f"{m.agg.upper()}({target})"


def value_expr(spec: MetricSpec) -> str:
    if spec.kind == "ratio":
        if not (spec.numerator and spec.denominator):
            raise ValueError("a ratio metric needs a numerator and a denominator")
        return f"CAST({_agg(spec.numerator)} AS DOUBLE) / NULLIF({_agg(spec.denominator)}, 0)"
    if not spec.measure:
        raise ValueError("a simple metric needs a measure")
    return _agg(spec.measure)


def compile_metric(
    spec: MetricSpec,
    *,
    start: str | None = None,
    end: str | None = None,
    by: list[str] | None = None,
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None,
    extra_filters: list[Filter] | None = None,
) -> str:
    """SQL against the dataset name (the guard maps it to the physical table)."""
    by = by or []
    for d in by:
        if d not in spec.dimensions:
            raise ValueError(f"{spec.name} cannot be split by {d}; allowed: {', '.join(spec.dimensions) or 'none'}")
    select: list[str] = []
    group: list[str] = []
    if grain:
        if not spec.time_column:
            raise ValueError(f"{spec.name} has no time column")
        select.append(f"date_trunc('{grain}', {_q(spec.time_column)}) AS period")
        group.append("period")
    for d in by:
        select.append(_q(d))
        group.append(_q(d))
    select.append(f"{value_expr(spec)} AS value")
    where = [_cond(f) for f in spec.filters + (extra_filters or [])]
    if start or end:
        if not spec.time_column:
            raise ValueError(f"{spec.name} has no time column for a period")
        if start:
            where.append(f"{_q(spec.time_column)} >= {_lit(start)}")
        if end:
            where.append(f"{_q(spec.time_column)} < {_lit(end)}")
    sql = f"SELECT {', '.join(select)} FROM {_q(spec.dataset)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(group) + " ORDER BY " + ", ".join(group)
    return sql


class SemanticService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard

    def define(self, actor: Principal, workspace_id: str, spec: MetricSpec, note: str = "") -> int:
        self.guard.require(actor, workspace_id, "metric.define", spec.name)
        db = self.stores.workspace(workspace_id)
        schema = db.query(
            "SELECT v.schema FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
            "AND v.version = d.current_version WHERE d.name = ?",
            [spec.dataset],
        )
        if not schema:
            raise ValueError(f"dataset {spec.dataset} does not exist")
        known = {c["name"] for c in json.loads(schema[0]["schema"])}
        missing = spec.columns() - known
        if missing:
            raise ValueError(f"{spec.dataset} has no column(s): {', '.join(sorted(missing))}")
        compile_metric(spec)  # raises on an invalid definition

        def tx(c):
            cur = c.execute("SELECT current_version FROM metrics WHERE name = ?", [spec.name]).fetchone()
            version = cur[0] + 1 if cur else 1
            c.execute(
                "INSERT INTO metric_versions (name, version, spec, note, created_by) VALUES (?, ?, ?, ?, ?)",
                [spec.name, version, spec.model_dump_json(), note, actor.user_id],
            )
            if cur:
                c.execute("UPDATE metrics SET current_version = ? WHERE name = ?", [version, spec.name])
            else:
                c.execute(
                    "INSERT INTO metrics (name, current_version, owner) VALUES (?, ?, ?)",
                    [spec.name, version, actor.user_id],
                )
            return version

        version = db.write(tx)
        self.guard.audit(actor, workspace_id, "metric.define", spec.name, "ok", {"version": version})
        return version

    def metrics(self, actor: Principal, workspace_id: str) -> list[MetricSpec]:
        self.guard.require(actor, workspace_id, "metric.read")
        rows = self.stores.workspace(workspace_id).query(
            "SELECT v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name "
            "AND v.version = m.current_version WHERE m.status = 'approved' ORDER BY m.name"
        )
        return [MetricSpec.model_validate_json(r["spec"]) for r in rows]

    def get(self, actor: Principal, workspace_id: str, name: str) -> MetricSpec:
        for m in self.metrics(actor, workspace_id):
            if m.name == name:
                return m
        raise KeyError(name)

    def set_term(self, actor: Principal, workspace_id: str, term: str, definition: str,
                 maps_to_metric: str | None = None, source: str | None = None) -> None:
        self.guard.require(actor, workspace_id, "glossary.write", term)
        self.stores.workspace(workspace_id).execute(
            "INSERT OR REPLACE INTO glossary (term, definition, maps_to_metric, status, source, updated_by) "
            "VALUES (?, ?, ?, 'approved', ?, ?)",
            [term.lower().strip(), definition, maps_to_metric, source, actor.user_id],
        )
        self.guard.audit(actor, workspace_id, "glossary.write", term, "ok")

    def glossary(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "metric.read")
        return self.stores.workspace(workspace_id).query(
            "SELECT term, definition, maps_to_metric, status, source FROM glossary ORDER BY term"
        )

    def resolve(self, actor: Principal, workspace_id: str, text: str) -> dict[str, Any]:
        """Find the approved metric(s) a request refers to.

        Returns {"metrics": [names], "ambiguous": {term: [names]}, "matched": {name: term}}.
        Longer phrases win over their substrings ("operating margin" beats "margin")."""
        lowered = " " + re.sub(r"[^a-z0-9%]+", " ", text.lower()) + " "
        metrics = self.metrics(actor, workspace_id)
        phrases: dict[str, set[str]] = {}
        for m in metrics:
            for p in {m.name.replace("_", " "), m.title.lower(), *[s.lower() for s in m.synonyms]}:
                phrases.setdefault(p.strip(), set()).add(m.name)
        for g in self.glossary(actor, workspace_id):
            if g["maps_to_metric"]:
                phrases.setdefault(g["term"], set()).add(g["maps_to_metric"])
        hits: list[tuple[str, set[str]]] = []
        for phrase in sorted(phrases, key=len, reverse=True):
            if f" {phrase} " in lowered:
                if any(phrase in longer for longer, _ in hits):
                    continue
                hits.append((phrase, phrases[phrase]))
        chosen: list[str] = []
        ambiguous: dict[str, list[str]] = {}
        matched: dict[str, str] = {}
        for phrase, names in hits:
            if len(names) > 1:
                ambiguous[phrase] = sorted(names)
                continue
            (name,) = names
            if name not in chosen:
                chosen.append(name)
                matched[name] = phrase
        return {"metrics": chosen, "ambiguous": ambiguous, "matched": matched}


__all__ = ["Filter", "Measure", "MetricSpec", "SemanticService", "compile_metric", "value_expr"]
