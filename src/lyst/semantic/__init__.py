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
    # semantic-layer metadata: who answers for the number, what one row of the dataset is, and its currency
    owner: str | None = None
    grain: list[str] = Field(default_factory=list)
    currency: str | None = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency must be an ISO 4217 code such as USD")
        return v

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
    def __init__(self, stores: Stores, guard: Guard, knowledge: Any = None) -> None:
        self.stores = stores
        self.guard = guard
        self.knowledge = knowledge

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
        missing = (spec.columns() | set(spec.grain)) - known
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
                # a new definition of a retired metric brings it back into use
                c.execute("UPDATE metrics SET current_version = ?, status = 'approved', replaced_by = NULL WHERE name = ?",
                          [version, spec.name])
            else:
                c.execute(
                    "INSERT INTO metrics (name, current_version, owner) VALUES (?, ?, ?)",
                    [spec.name, version, actor.user_id],
                )
            return version

        version = db.write(tx)
        if self.knowledge:
            self.knowledge.record_metric(actor, workspace_id, spec)
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

    # -- lifecycle and history -------------------------------------------------------------

    def history(self, actor: Principal, workspace_id: str, name: str) -> list[dict[str, Any]]:
        """Every version of a metric, newest first, with what changed against the version before it."""
        self.guard.require(actor, workspace_id, "metric.read", name)
        rows = self.stores.workspace(workspace_id).query(
            "SELECT version, spec, note, created_by, created_at FROM metric_versions WHERE name = ? ORDER BY version", [name])
        if not rows:
            raise KeyError(name)
        out, prev = [], None
        for r in rows:
            spec = json.loads(r["spec"])
            changes = [] if prev is None else _diff(prev, spec)
            out.append({"version": r["version"], "note": r["note"], "created_by": r["created_by"], "created_at": r["created_at"],
                        "changes": changes, "summary": "first definition" if prev is None else
                        ("; ".join(c["text"] for c in changes) or "no change to the definition"),
                        "sql": compile_metric(MetricSpec.model_validate(spec))})
            prev = spec
        return list(reversed(out))

    def deprecate(self, actor: Principal, workspace_id: str, name: str, *, replaced_by: str | None = None, note: str = "") -> dict[str, Any]:
        """Retire a metric from use. Requests that name it resolve to its replacement, or are told it was retired."""
        self.guard.require(actor, workspace_id, "metric.define", name)
        db = self.stores.workspace(workspace_id)
        if not db.scalar("SELECT count(*) FROM metrics WHERE name = ?", [name]):
            raise KeyError(name)
        if replaced_by:
            if replaced_by == name or not db.scalar("SELECT count(*) FROM metrics WHERE name = ? AND status = 'approved'", [replaced_by]):
                raise ValueError(f"{replaced_by} is not an approved metric")
        db.execute("UPDATE metrics SET status = 'deprecated', replaced_by = ? WHERE name = ?", [replaced_by, name])
        self.guard.audit(actor, workspace_id, "metric.deprecate", name, "ok", {"replaced_by": replaced_by, "note": note})
        return {"name": name, "status": "deprecated", "replaced_by": replaced_by}

    def deprecated(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        self.guard.require(actor, workspace_id, "metric.read")
        return self.stores.workspace(workspace_id).query(
            "SELECT m.name, m.replaced_by, v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name "
            "AND v.version = m.current_version WHERE m.status = 'deprecated' ORDER BY m.name")

    def audit(self, actor: Principal, workspace_id: str) -> list[dict[str, Any]]:
        """Problems in the semantic layer itself, each with a severity, the items involved and a suggested fix:
        duplicate definitions, the same measure computed from two sources, synonyms that make requests ambiguous,
        glossary terms pointing at missing or retired metrics or left undefined, metrics reading columns their
        dataset no longer has, and definitions missing an owner, a description or a currency."""
        self.guard.require(actor, workspace_id, "metric.read")
        db = self.stores.workspace(workspace_id)
        metrics = self.metrics(actor, workspace_id)
        issues: list[dict[str, Any]] = []
        by_expr: dict[tuple, list[str]] = {}
        by_measure: dict[str, list[MetricSpec]] = {}
        for m in metrics:
            by_expr.setdefault((m.dataset, value_expr(m), tuple(sorted(_cond(f) for f in m.filters))), []).append(m.name)
            measure_key = value_expr(m.model_copy(update={"dataset": "x"})) + "|" + "|".join(sorted(_cond(f) for f in m.filters))
            by_measure.setdefault(measure_key, []).append(m)
        for names in by_expr.values():
            if len(names) > 1:
                issues.append({"severity": "high", "kind": "duplicate_definition", "items": sorted(names),
                               "text": f"{' and '.join(sorted(names))} compute the same number the same way",
                               "fix": "deprecate one and point it at the other, or add synonyms instead of a second metric"})
        for group in by_measure.values():
            sources = sorted({m.dataset for m in group})
            if len(sources) > 1:
                issues.append({"severity": "medium", "kind": "conflicting_source", "items": sorted(m.name for m in group),
                               "text": f"the same calculation is defined on {', '.join(sources)}; answers will differ if the sources disagree",
                               "fix": "choose the authoritative source and deprecate the other definition"})
        phrases: dict[str, set[str]] = {}
        for m in metrics:
            for p in {m.name.replace("_", " "), m.title.lower(), *[s.lower() for s in m.synonyms]}:
                phrases.setdefault(p.strip(), set()).add(m.name)
        for phrase, names in sorted(phrases.items()):
            if len(names) > 1:
                issues.append({"severity": "medium", "kind": "ambiguous_term", "items": sorted(names),
                               "text": f"'{phrase}' names {' and '.join(sorted(names))}, so a request using it is asked to choose",
                               "fix": "keep the synonym on one metric only"})
        approved = {m.name for m in metrics}
        retired = {r["name"]: r["replaced_by"] for r in db.query("SELECT name, replaced_by FROM metrics WHERE status = 'deprecated'")}
        for g in self.glossary(actor, workspace_id):
            target = g["maps_to_metric"]
            if target and target in retired:
                issues.append({"severity": "medium", "kind": "term_points_to_retired_metric", "items": [g["term"], target],
                               "text": f"the glossary maps '{g['term']}' to {target}, which is retired"
                                       + (f" in favour of {retired[target]}" if retired[target] else ""),
                               "fix": f"map '{g['term']}' to {retired[target]}" if retired[target] else "map the term to an approved metric"})
            elif target and target not in approved:
                issues.append({"severity": "high", "kind": "term_points_to_missing_metric", "items": [g["term"], target],
                               "text": f"the glossary maps '{g['term']}' to {target}, which does not exist", "fix": "fix the mapping"})
            if re.search(r"not defined|tbd|to be defined|todo", g["definition"], re.I):
                issues.append({"severity": "low", "kind": "undefined_term", "items": [g["term"]],
                               "text": f"'{g['term']}' has no agreed definition yet", "fix": "write the definition or remove the term"})
            if not g.get("source"):
                issues.append({"severity": "low", "kind": "missing_provenance", "items": [g["term"]],
                               "text": f"'{g['term']}' does not say where its definition comes from", "fix": "add the source document or owner"})
        schemas = {r["name"]: {c["name"] for c in json.loads(r["schema"])} for r in db.query(
            "SELECT d.name, v.schema FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version")}
        for m in metrics:
            if m.dataset not in schemas:
                issues.append({"severity": "high", "kind": "missing_dataset", "items": [m.name], "text": f"{m.name} reads {m.dataset}, which no longer exists",
                               "fix": "redefine the metric on its new source"})
                continue
            gone = sorted((m.columns() | set(m.grain)) - schemas[m.dataset])
            if gone:
                issues.append({"severity": "high", "kind": "stale_definition", "items": [m.name],
                               "text": f"{m.name} reads {', '.join(gone)}, no longer in the current version of {m.dataset}",
                               "fix": "redefine the metric on the new columns, or restore them upstream"})
            missing = [f for f, ok in (("a description", bool(m.description)), ("an owner", bool(m.owner)),
                                       ("a currency", m.format != "currency" or bool(m.currency))) if not ok]
            if missing:
                issues.append({"severity": "low", "kind": "incomplete_definition", "items": [m.name],
                               "text": f"{m.name} has no {' or '.join(x.split(' ', 1)[1] for x in missing)}",
                               "fix": "add " + ", ".join(missing)})
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(issues, key=lambda i: (order[i["severity"]], i["kind"], i["items"]))

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
        approved = {m.name for m in metrics}
        retired = self.deprecated(actor, workspace_id)
        successor = {r["name"]: r["replaced_by"] for r in retired if r["replaced_by"] in approved}
        for g in self.glossary(actor, workspace_id):
            target = g["maps_to_metric"]
            target = target if target in approved else successor.get(target)
            if target:
                phrases.setdefault(g["term"], set()).add(target)
        for r in retired:
            # a retired metric's words lead to its replacement, unless an approved metric already uses them
            if r["replaced_by"] in approved:
                old = MetricSpec.model_validate_json(r["spec"])
                for p in {old.name.replace("_", " "), old.title.lower(), *[s.lower() for s in old.synonyms]}:
                    if p.strip() not in phrases:
                        phrases[p.strip()] = {r["replaced_by"]}
        hits: list[tuple[str, set[str]]] = []
        for phrase in sorted(phrases, key=len, reverse=True):
            if any(f" {variant} " in lowered for variant in (phrase, phrase + "s", phrase + "es")):
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


def _diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level changes between two stored metric specs, in plain words."""
    out = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va == vb or (not va and not vb):
            continue
        label = key.replace("_", " ")
        if key in ("measure", "numerator", "denominator", "filters"):
            text = f"{label} changed"
        elif isinstance(va, list) or isinstance(vb, list):
            added = sorted(set(map(str, vb or [])) - set(map(str, va or [])))
            removed = sorted(set(map(str, va or [])) - set(map(str, vb or [])))
            text = f"{label}: " + ", ".join([*(f"+{x}" for x in added), *(f"-{x}" for x in removed)])
        else:
            text = f"{label}: {va!r} → {vb!r}"
        out.append({"field": key, "before": va, "after": vb, "text": text})
    return out


__all__ = ["Filter", "Measure", "MetricSpec", "SemanticService", "compile_metric", "value_expr"]
