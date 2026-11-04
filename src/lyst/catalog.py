# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Data intelligence: what each dataset is, how datasets relate, how trustworthy they are, and where a metric comes from.

Every governed dataset version gets **insights**, computed in DuckDB on the stored table (never by a model):

* **Grain and keys**: the smallest set of non-measure columns that identifies a row, single-column key candidates,
  and how many rows are duplicated at the grain when no exact grain exists.
* **Column semantics**: identifier, date, amount, percent, count, category, geography, free text, personal data.
* **Profiling beyond the basic profile**: robust outliers (median absolute deviation), where missing values
  concentrate (a dimension member that holds most of them), columns that go missing together, gaps in the time
  series (overall and per series at the grain), freshness of the latest period.
* **Quality risks**, each with a severity and a plain sentence.

Across datasets, ``discover`` infers **relationships**: a column whose values are contained in another dataset's
key is a foreign-key candidate; two datasets sharing a dimension at the same values are joinable on it; the same
column name with a different type or disjoint values is an **inconsistent definition**; two datasets that carry the
same measure at the same grain **overlap**, and their totals are reconciled so the difference is a number, not a
guess. Relationships are written to the knowledge graph with their evidence, so lineage walks see them.

``explain_metric`` answers "where does this metric come from?": the definition and its compiled SQL, the dataset
version and its source (a file or a connection), the columns, the contract and its last check, quality risks on the
columns it reads, and everything downstream (analyses, dashboards and other artifacts, workflows, monitors).
``impact`` lists the downstream assets of a dataset or of some of its columns, which data contracts use when an
upstream change breaks expectations. ``search`` finds datasets, columns, metrics, glossary terms, workflows,
dashboards and documents for a business question, permission-aware, and connects each hit to its neighbours.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import re
from datetime import date, datetime
from typing import Any

from .security import Guard, Principal
from .semantic import compile_metric
from .store import Stores

log = logging.getLogger("lyst.catalog")

NON_MEASURE = ("datetime", "dimension", "id", "flag")
_AMOUNT = re.compile(r"revenue|sales|amount|price|cost|cogs|opex|profit|spend|income|fee|budget|salary|value|mrr|arr", re.I)
_PCT = re.compile(r"(^|_)(pct|percent|rate|ratio|share|margin)($|_)", re.I)
_COUNT = re.compile(r"(^|_)(count|qty|quantity|units|tickets|orders|agents|headcount|visits|n)($|_)|s$", re.I)
_GEO = re.compile(r"country|state|province|city|region|postal|zip|county|market|territory", re.I)
_TOKEN = re.compile(r"[a-z][a-z0-9]{1,}")
SAMPLE_ROWS = 100_000
GRAIN_COLUMNS = 8
_STOP = {"the", "and", "for", "with", "our", "what", "which", "where", "does", "how", "show", "find", "everything",
         "related", "about", "from", "come", "comes", "into", "all", "any", "this", "that", "are", "was", "is", "of",
         "in", "on", "to", "by", "me", "us", "we", "do", "a", "an", "it", "its"}


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _family(duck_type: str) -> str:
    t = duck_type.lower()
    if any(k in t for k in ("int", "decimal", "double", "float", "real", "numeric", "hugeint")):
        return "number"
    if "timestamp" in t or t.startswith("date") or t == "time":
        return "time"
    if "bool" in t:
        return "boolean"
    return "text"


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()[:10] if isinstance(v, date) and not isinstance(v, datetime) else v.isoformat()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def semantic_type(name: str, role: str, dtype: str, issues: list[str]) -> str:
    """A business meaning for a column, from its profiled role, its type and its name."""
    if any("PII" in i for i in issues):
        return "personal_data"
    if role == "datetime":
        return "date"
    if role == "id":
        return "identifier"
    if role == "text":
        return "free_text"
    if role == "flag":
        return "flag"
    if role == "measure":
        if _PCT.search(name):
            return "percent"
        if _AMOUNT.search(name):
            return "amount"
        if _COUNT.search(name) and "int" in dtype.lower():
            return "count"
        return "quantity"
    if _GEO.search(name):
        return "geography"
    return "category"


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower().replace("_", " ")):
        if t in _STOP:
            continue
        out.append(t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t)
    return out


class CatalogService:
    def __init__(self, stores: Stores, guard: Guard, data, semantic, knowledge=None) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.semantic = semantic
        self.knowledge = knowledge
        self.contracts = None  # set by the composition root (contracts read insights; the catalog reads contract state)

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- per-dataset insights -------------------------------------------------------------------

    def _current(self, ws: str, name: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT d.name, d.title, d.description, d.owner, d.classification, d.updated_at, v.version, v.physical_table, "
               "v.source, v.row_count, v.schema, v.profile, v.created_at AS refreshed_at "
               "FROM datasets d JOIN dataset_versions v ON v.dataset = d.name AND v.version = d.current_version")
        rows = self._db(ws).query(sql + (" WHERE d.name = ?" if name else "") + " ORDER BY d.name", [name] if name else [])
        for r in rows:
            r["schema"] = json.loads(r["schema"])
            r["profile"] = json.loads(r["profile"] or "{}")
        return rows

    def _version_row(self, ws: str, name: str, version: int) -> dict[str, Any]:
        rows = self._db(ws).query(
            "SELECT d.name, d.title, d.owner, v.version, v.physical_table, v.source, v.row_count, v.schema, v.profile, "
            "v.created_at AS refreshed_at FROM datasets d JOIN dataset_versions v ON v.dataset = d.name "
            "WHERE d.name = ? AND v.version = ?", [name, version])
        if not rows:
            raise KeyError(f"{name} v{version}")
        r = rows[0]
        r["schema"], r["profile"] = json.loads(r["schema"]), json.loads(r["profile"] or "{}")
        return r

    def compute_insights(self, ws: str, name: str, version: int | None = None) -> dict[str, Any]:
        """Compute and store insights for a dataset version (the current one by default). System work: no actor."""
        row = self._version_row(ws, name, version) if version else self._current(ws, name)[0]
        insights = _analyse(self._db(ws), row)
        self._db(ws).execute("INSERT OR REPLACE INTO dataset_insights (dataset, version, insights) VALUES (?, ?, ?)",
                             [name, row["version"], json.dumps(insights, default=_jsonable)])
        return insights

    def after_ingest(self, actor: Principal, ws: str, result: dict[str, Any]) -> None:
        """Ingestion hook: insights for the new version. Never fails the ingest."""
        if result.get("unchanged"):
            return
        try:
            self.compute_insights(ws, result["dataset"], result["version"])
            self._discover(actor, ws)
        except Exception:  # noqa: BLE001 - insights are advisory; the data is already committed
            log.exception("insights for %s v%s failed", result.get("dataset"), result.get("version"))

    def _stored(self, ws: str, name: str, version: int) -> dict[str, Any]:
        raw = self._db(ws).scalar("SELECT insights FROM dataset_insights WHERE dataset = ? AND version = ?", [name, version])
        return json.loads(raw) if raw else self.compute_insights(ws, name, version)

    def _visibility(self, actor: Principal, ws: str):
        policy = self.data.policy_for(actor, ws)
        return policy.restricted_columns, set(policy.row_filters)

    def _redact(self, insights: dict[str, Any], dataset: str, restricted: dict[str, set[str]], row_limited: set[str]) -> dict[str, Any]:
        """Hide columns the reader may not see and value-level detail on datasets whose rows they only partly see."""
        hidden = restricted.get(dataset, set())
        out = json.loads(json.dumps(insights))
        out["columns"] = [c for c in out.get("columns", []) if c["name"] not in hidden]
        out["risks"] = [r for r in out.get("risks", []) if not set(r.get("columns", [])) & hidden]
        if dataset in row_limited:
            for c in out["columns"]:
                c.pop("top", None)
                c.pop("null_concentration", None)
            out["value_detail"] = "hidden: you see only some rows of this dataset"
        return out

    def insights(self, actor: Principal, ws: str, name: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read", name)
        rows = self._current(ws, name)
        if not rows:
            raise KeyError(name)
        restricted, row_limited = self._visibility(actor, ws)
        out = self._redact(self._stored(ws, name, rows[0]["version"]), name, restricted, row_limited)
        out["relationships"] = [r for r in self.relationships(actor, ws) if name in (r["from_dataset"], r["to_dataset"])]
        if self.contracts:
            out["contract"] = self.contracts.status(ws, name)
        return out

    # -- across datasets ------------------------------------------------------------------------

    def discover(self, actor: Principal, ws: str) -> dict[str, Any]:
        """Insights for every current dataset, then relationships between them, recorded in the knowledge graph."""
        self.guard.require(actor, ws, "data.ingest")
        out = self._discover(actor, ws)
        self.guard.audit(actor, ws, "catalog.discover", None, "ok", {"datasets": out["datasets"], "relationships": len(out["relationships"])})
        return out

    def _discover(self, actor: Principal, ws: str) -> dict[str, Any]:
        current = self._current(ws)
        per = {d["name"]: self._stored(ws, d["name"], d["version"]) for d in current}
        rels = _relationships(self._db(ws), current, per)
        self._db(ws).execute("DELETE FROM relations WHERE source = 'discovery'")
        if self.knowledge:
            for r in rels:
                if r["kind"] in ("foreign_key", "shared_dimension"):
                    self.knowledge.relate(actor, ws, "column", f"{r['from_dataset']}.{r['from_column']}", "references" if r["kind"] == "foreign_key"
                                          else "joins", "column", f"{r['to_dataset']}.{r['to_column']}", evidence=r["evidence"], source="discovery")
                    self.knowledge.relate(actor, ws, "dataset", r["from_dataset"], "joins", "dataset", r["to_dataset"],
                                          evidence={"on": r["from_column"], "kind": r["kind"]}, source="discovery")
                elif r["kind"] == "overlap":
                    self.knowledge.relate(actor, ws, "dataset", r["from_dataset"], "overlaps", "dataset", r["to_dataset"],
                                          evidence=r["evidence"], source="discovery")
        self._db(ws).execute("INSERT OR REPLACE INTO dataset_insights (dataset, version, insights) VALUES (?, ?, ?)",
                             ["__relationships__", 0, json.dumps(rels, default=_jsonable)])
        return {"datasets": len(current), "relationships": rels,
                "risks": sum(len(i.get("risks", [])) for i in per.values())}

    def relationships(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        raw = self._db(ws).scalar("SELECT insights FROM dataset_insights WHERE dataset = '__relationships__' AND version = 0")
        rels = json.loads(raw) if raw else []
        restricted, row_limited = self._visibility(actor, ws)
        out = []
        for r in rels:
            cols = {(r["from_dataset"], r.get("from_column")), (r["to_dataset"], r.get("to_column"))}
            if any(c and c in restricted.get(d, set()) for d, c in cols):
                continue
            if {r["from_dataset"], r["to_dataset"]} & row_limited:
                r = {**r, "evidence": {k: v for k, v in r["evidence"].items() if k not in ("totals", "examples")}}
            out.append(r)
        return out

    def overview(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        """One row per dataset: grain, keys, freshness, risks, contract, owner, trust and usage."""
        self.guard.require(actor, ws, "data.read")
        restricted, row_limited = self._visibility(actor, ws)
        used = {}
        for m in self.semantic.metrics(actor, ws):
            used.setdefault(m.dataset, []).append(m.name)
        out = []
        for d in self._current(ws):
            ins = self._redact(self._stored(ws, d["name"], d["version"]), d["name"], restricted, row_limited)
            contract = self.contracts.status(ws, d["name"]) if self.contracts else None
            risks = ins.get("risks", [])
            out.append({"name": d["name"], "title": d["title"], "description": d["description"], "owner": d["owner"],
                        "classification": d["classification"], "version": d["version"], "rows": d["row_count"],
                        "source": d["source"], "refreshed_at": d["refreshed_at"], "grain": ins.get("grain"),
                        "keys": ins.get("keys", []), "freshness": ins.get("freshness"), "summary": ins.get("summary"),
                        "risks": {"high": sum(r["severity"] == "high" for r in risks), "medium": sum(r["severity"] == "medium" for r in risks),
                                  "low": sum(r["severity"] == "low" for r in risks)},
                        "contract": contract, "metrics": sorted(used.get(d["name"], [])),
                        "trusted": bool(used.get(d["name"])) and bool(contract and contract.get("outcome") in ("pass", "warn"))})
        return out

    # -- lineage and impact ---------------------------------------------------------------------

    def impact(self, actor: Principal, ws: str, dataset: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
        """Downstream assets of a dataset, or of some of its columns: metrics, workflows, dashboards and other
        artifacts, monitors and saved analyses, each with why it is affected."""
        self.guard.require(actor, ws, "data.read", dataset)
        return impact_of(self._db(ws), self.semantic, actor, ws, dataset, columns)

    def explain_metric(self, actor: Principal, ws: str, name: str) -> dict[str, Any]:
        """Where a metric comes from and what depends on it."""
        self.guard.require(actor, ws, "metric.read", name)
        db = self._db(ws)
        meta = db.query("SELECT name, current_version, status, owner, replaced_by, created_at FROM metrics WHERE name = ?", [name])
        if not meta:
            raise KeyError(name)
        meta = meta[0]
        spec_raw = db.scalar("SELECT spec FROM metric_versions WHERE name = ? AND version = ?", [name, meta["current_version"]])
        from .semantic import MetricSpec  # noqa: PLC0415

        spec = MetricSpec.model_validate_json(spec_raw)
        ds = self._current(ws, spec.dataset)
        dataset = None
        quality: list[dict[str, Any]] = []
        if ds:
            d = ds[0]
            restricted, row_limited = self._visibility(actor, ws)
            ins = self._redact(self._stored(ws, d["name"], d["version"]), d["name"], restricted, row_limited)
            source = d["source"]
            origin = {"kind": "file", "name": source}
            if source.count(":") >= 2 and not source.lower().endswith((".csv", ".parquet", ".tsv")):
                kind, conn, table = source.split(":", 2)
                origin = {"kind": "connection", "connector": kind, "connection": conn, "table": table}
            dataset = {"name": d["name"], "title": d["title"], "version": d["version"], "rows": d["row_count"],
                       "owner": d["owner"], "refreshed_at": d["refreshed_at"], "origin": origin, "grain": ins.get("grain"),
                       "freshness": ins.get("freshness")}
            cols = spec.columns()
            quality = [r for r in ins.get("risks", []) if set(r.get("columns", [])) & cols or r.get("scope") == "table"]
        upstream = []
        for r in db.query("SELECT from_kind, from_name, rel, to_kind, to_name, evidence FROM relations WHERE source = 'discovery' AND "
                          "from_kind = 'dataset' AND (from_name = ? OR to_name = ?)", [spec.dataset, spec.dataset]):
            other = r["to_name"] if r["from_name"] == spec.dataset else r["from_name"]
            upstream.append({"dataset": other, "rel": r["rel"], "evidence": json.loads(r["evidence"] or "{}")})
        return {
            "metric": spec.name, "title": spec.title, "description": spec.description, "status": meta["status"],
            "replaced_by": meta["replaced_by"], "version": meta["current_version"], "owner": spec.owner or meta["owner"],
            "kind": spec.kind, "unit": spec.unit, "currency": spec.currency, "grain": spec.grain,
            "sql": compile_metric(spec), "columns": sorted(spec.columns()), "dataset": dataset,
            "contract": self.contracts.status(ws, spec.dataset) if self.contracts else None,
            "quality": quality, "related_sources": upstream,
            "downstream": [i for i in impact_of(db, self.semantic, actor, ws, spec.dataset, sorted(spec.columns()))
                           if i["kind"] != "metric" and name in i.get("metrics", [])],
            "history": self.semantic.history(actor, ws, name),
        }

    # -- search ---------------------------------------------------------------------------------

    def search(self, actor: Principal, ws: str, query: str, *, limit: int = 20) -> dict[str, Any]:
        """Datasets, columns, metrics, glossary terms, workflows, dashboards and documents for a question, ranked by how
        many of its words they match (names and titles count more than descriptions), trusted definitions first among
        equals, each with its directly connected assets."""
        self.guard.require(actor, ws, "data.read")
        q = set(tokens(query))
        if not q:
            return {"query": query, "results": []}
        db = self._db(ws)
        restricted, _ = self._visibility(actor, ws)
        hits: list[dict[str, Any]] = []

        def score(primary: str, secondary: str = "") -> float:
            p, s = set(tokens(primary)), set(tokens(secondary))
            return 3.0 * len(q & p) + 1.0 * len(q & (s - p))

        metrics = self.semantic.metrics(actor, ws)
        glossary = self.semantic.glossary(actor, ws)
        expanded: dict[str, str] = {}
        for g in glossary:
            if g["maps_to_metric"] and set(tokens(g["term"])) and set(tokens(g["term"])) <= q:
                expanded[g["maps_to_metric"]] = g["term"]
        for m in metrics:
            s = score(" ".join([m.name, m.title, *m.synonyms]), m.description + " " + m.dataset)
            if m.name in expanded:
                s += 3.0
            if s:
                hits.append({"kind": "metric", "name": m.name, "title": m.title, "score": s + 0.5,
                             "why": f"approved metric on {m.dataset}" + (f"; the glossary maps '{expanded[m.name]}' to it" if m.name in expanded else ""),
                             "related": [{"kind": "dataset", "name": m.dataset}]})
        by_dataset: dict[str, list[str]] = {}
        for m in metrics:
            by_dataset.setdefault(m.dataset, []).append(m.name)
        for d in self._current(ws):
            hidden = restricted.get(d["name"], set())
            cols = [c["name"] for c in d["schema"] if c["name"] not in hidden]
            s = score(f"{d['name']} {d['title']}", f"{d['description'] or ''} {' '.join(cols)}")
            if s:
                hits.append({"kind": "dataset", "name": d["name"], "title": d["title"], "score": s + (0.5 if by_dataset.get(d["name"]) else 0),
                             "why": f"{d['row_count']:,} rows, v{d['version']}" + (", read by approved metrics" if by_dataset.get(d["name"]) else ""),
                             "related": [{"kind": "metric", "name": n} for n in sorted(by_dataset.get(d["name"], []))]})
            for c in cols:
                cs = score(c)
                if cs >= 3:
                    hits.append({"kind": "column", "name": f"{d['name']}.{c}", "title": c.replace("_", " "), "score": cs - 0.5,
                                 "why": f"column of {d['title']}", "related": [{"kind": "dataset", "name": d["name"]}]})
        for g in glossary:
            s = score(g["term"], g["definition"])
            if s:
                hits.append({"kind": "term", "name": g["term"], "title": g["term"], "score": s, "why": g["definition"][:160],
                             "related": [{"kind": "metric", "name": g["maps_to_metric"]}] if g["maps_to_metric"] else []})
        for w in db.query("SELECT id, title, intent, deps FROM workflows WHERE status = 'active' QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1"):
            s = score(w["title"], w["intent"])
            if s:
                deps = json.loads(w["deps"])
                hits.append({"kind": "workflow", "name": w["id"], "title": w["title"], "score": s, "why": "saved, verified analysis",
                             "related": [{"kind": "metric", "name": n} for n in deps.get("metrics", {})]})
        for b in db.query("SELECT id, title, design FROM dashboards WHERE status = 'published'"):
            design = json.loads(b["design"])
            s = score(b["title"], " ".join(m.get("title", "") for m in design.get("metrics", [])))
            if s:
                hits.append({"kind": "dashboard", "name": b["id"], "title": b["title"], "score": s, "why": "published dashboard",
                             "related": [{"kind": "metric", "name": m["name"]} for m in design.get("metrics", [])]})
        for a in db.query("SELECT id, question FROM analyses ORDER BY created_at DESC LIMIT 500"):
            s = score(a["question"])
            if s >= 3:
                hits.append({"kind": "analysis", "name": a["id"], "title": a["question"][:120], "score": s - 1, "why": "earlier analysis",
                             "related": []})
        if self.knowledge:
            try:
                for p in self.knowledge.search(actor, ws, query, limit=5):
                    hits.append({"kind": "document", "name": p.get("document_id") or p.get("id"), "title": p.get("title", "document"),
                                 "score": 2.0 * float(p.get("score", 1.0)) / (1 + float(p.get("score", 1.0))) * 3,
                                 "why": (p.get("text") or "")[:160], "related": []})
            except Exception:  # noqa: BLE001 - document search is one source among several
                log.exception("document search failed")
        hits.sort(key=lambda h: (-h["score"], h["kind"], h["name"]))
        return {"query": query, "results": hits[:limit]}


# -- analysis (pure functions over a workspace database) -----------------------------------------


def _analyse(db, row: dict[str, Any]) -> dict[str, Any]:
    table = f'data.{_q(row["physical_table"])}'
    prof_cols = {c["name"]: c for c in row["profile"].get("columns", [])}
    schema = row["schema"]
    rows = int(row["row_count"])
    columns: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    with db.read() as cur:
        sample = f"(SELECT * FROM {table} USING SAMPLE {SAMPLE_ROWS} ROWS)" if rows > SAMPLE_ROWS else table
        for col in schema:
            name, dtype = col["name"], col["type"]
            p = prof_cols.get(name, {})
            role = p.get("role", "dimension")
            info: dict[str, Any] = {"name": name, "type": dtype, "role": role,
                                    "semantic_type": semantic_type(name, role, dtype, p.get("issues", [])),
                                    "nulls": p.get("nulls", 0.0), "distinct": p.get("unique"), "issues": p.get("issues", [])}
            if role in ("dimension", "flag") and p.get("top"):
                info["top"] = p["top"][:8]
            if role == "measure" and _family(dtype) == "number":
                med, mad, n_out = cur.execute(
                    f"WITH s AS (SELECT CAST({_q(name)} AS DOUBLE) AS x FROM {table} WHERE {_q(name)} IS NOT NULL), "
                    "m AS (SELECT median(x) AS med FROM s), d AS (SELECT median(abs(x - m.med)) AS mad FROM s, m) "
                    "SELECT m.med, d.mad, (SELECT count(*) FROM s, m, d WHERE d.mad > 0 AND abs(s.x - m.med) / (1.4826 * d.mad) > 5) "
                    "FROM m, d").fetchone()
                info["robust"] = {"median": med, "mad": mad, "outliers": int(n_out or 0)}
                if n_out:
                    risks.append({"severity": "low", "kind": "outliers", "columns": [name],
                                  "text": f"{int(n_out):,} values of {name} are more than 5 robust deviations from the median"})
                neg = cur.execute(f"SELECT count(*) FROM {table} WHERE {_q(name)} < 0").fetchone()[0]
                if neg and info["semantic_type"] == "amount" and not re.search(r"income|profit|margin|net|change|delta", name):
                    risks.append({"severity": "medium", "kind": "negative_amount", "columns": [name],
                                  "text": f"{neg:,} negative value{'s' if neg != 1 else ''} in {name}, an amount that is normally positive"})
            columns.append(info)

        dims = [c for c in columns if c["role"] in ("dimension", "flag") and (c["distinct"] or 0) <= 60]
        # where missing values concentrate, and columns that go missing together
        nullable = [c for c in columns if (c["nulls"] or 0) > 0]
        for c in nullable:
            n_null = cur.execute(f"SELECT count(*) FROM {table} WHERE {_q(c['name'])} IS NULL").fetchone()[0]
            c["null_rows"] = int(n_null)
            best = None
            for d in dims:
                if d["name"] == c["name"]:
                    continue
                r = cur.execute(f"SELECT CAST({_q(d['name'])} AS VARCHAR), count(*) FROM {table} WHERE {_q(c['name'])} IS NULL "
                                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 1").fetchone()
                if r and n_null and r[1] / n_null >= 0.8 and (d["distinct"] or 0) > 1 and (best is None or r[1] > best[2]):
                    best = (d["name"], r[0], r[1])
            if best and n_null:
                c["null_concentration"] = {"column": best[0], "value": best[1], "share": round(best[2] / n_null, 3)}
            severity = "high" if c["nulls"] > 0.2 else "medium"
            where = f"; {best[2]:,} of them where {best[0]} = {best[1]}" if best else ""
            risks.append({"severity": severity, "kind": "missing_values", "columns": [c["name"]],
                          "text": f"{n_null:,} missing value{'s' if n_null != 1 else ''} in {c['name']} ({c['nulls']:.1%}){where}"})
        together = []
        for a, b in itertools.combinations([c["name"] for c in nullable], 2):
            both, either = cur.execute(f"SELECT count(*) FILTER (WHERE {_q(a)} IS NULL AND {_q(b)} IS NULL), "
                                       f"count(*) FILTER (WHERE {_q(a)} IS NULL OR {_q(b)} IS NULL) FROM {table}").fetchone()
            if either and both / either >= 0.8:
                together.append({"columns": [a, b], "share": round(both / either, 3)})
        # grain and keys
        candidates = [c["name"] for c in columns if c["role"] in NON_MEASURE and _family(c["type"]) != "number" or c["role"] == "id"]
        candidates = sorted(candidates, key=lambda n: (next(c["role"] for c in columns if c["name"] == n) != "datetime",
                                                         next(c["distinct"] or 0 for c in columns if c["name"] == n)))[:GRAIN_COLUMNS]
        keys = []
        for n in candidates:
            if next(c["nulls"] for c in columns if c["name"] == n) == 0:
                distinct = cur.execute(f"SELECT count(DISTINCT {_q(n)}) FROM {table}").fetchone()[0]
                if distinct == rows and rows:
                    keys.append(n)
        grain, dup_at_grain = None, 0
        if keys:
            grain = [keys[0]]
        elif candidates and rows:
            for size in range(2, min(4, len(candidates)) + 1):
                for combo in itertools.combinations(candidates, size):
                    cols = ", ".join(_q(c) for c in combo)
                    if cur.execute(f"SELECT count(*) = count(DISTINCT ({cols})) FROM {sample}").fetchone()[0] and \
                            cur.execute(f"SELECT count(*) = count(DISTINCT ({cols})) FROM {table}").fetchone()[0]:
                        grain = list(combo)
                        break
                if grain:
                    break
            if not grain:
                cols = ", ".join(_q(c) for c in candidates)
                dup_at_grain = int(cur.execute(f"SELECT count(*) - count(DISTINCT ({cols})) FROM {table}").fetchone()[0])
                example = cur.execute(f"SELECT {cols}, count(*) AS n FROM {table} GROUP BY ALL HAVING count(*) > 1 ORDER BY n DESC LIMIT 1").fetchone()
                ex = ", ".join(f"{c} = {_jsonable(v)}" for c, v in zip(candidates, example[:-1], strict=False)) if example else ""
                risks.append({"severity": "high", "kind": "duplicates_at_grain", "columns": candidates, "scope": "table",
                              "text": f"no combination of {', '.join(candidates)} identifies a row: {dup_at_grain:,} row{' repeats' if dup_at_grain == 1 else 's repeat'} "
                                      f"(for example {ex}); sums over this table may double count"})
        # time-series continuity and freshness
        time_cols = [c["name"] for c in columns if c["role"] == "datetime"]
        freshness = continuity = None
        if time_cols:
            t = time_cols[0]
            g = _time_grain(cur, table, t)
            lo, hi, present = cur.execute(f"SELECT min(CAST({_q(t)} AS DATE)), max(CAST({_q(t)} AS DATE)), "
                                          f"count(DISTINCT date_trunc('{g}', CAST({_q(t)} AS DATE))) FROM {table}").fetchone()
            if lo and hi:
                expected = cur.execute(f"SELECT count(*) FROM range(date_trunc('{g}', DATE '{lo}'), date_trunc('{g}', DATE '{hi}') + INTERVAL 1 {g}, "
                                       f"INTERVAL 1 {g})").fetchone()[0]
                gaps = [str(r[0])[:10] for r in cur.execute(
                    f"SELECT p FROM (SELECT unnest(range(date_trunc('{g}', DATE '{lo}'), date_trunc('{g}', DATE '{hi}') + INTERVAL 1 {g}, INTERVAL 1 {g})) AS p) "
                    f"WHERE p NOT IN (SELECT DISTINCT date_trunc('{g}', CAST({_q(t)} AS DATE)) FROM {table}) ORDER BY p LIMIT 24").fetchall()]
                series_cols = [c for c in (grain or []) if c != t]
                incomplete = 0
                if series_cols:
                    sc = ", ".join(_q(c) for c in series_cols)
                    incomplete = int(cur.execute(f"SELECT count(*) FROM (SELECT {sc}, count(DISTINCT date_trunc('{g}', CAST({_q(t)} AS DATE))) AS n "
                                                 f"FROM {table} GROUP BY ALL) WHERE n < {int(present)}").fetchone()[0])
                continuity = {"column": t, "grain": g, "start": str(lo), "end": str(hi), "periods": int(present), "expected": int(expected),
                              "gaps": gaps, "incomplete_series": incomplete, "series_by": series_cols}
                if gaps:
                    risks.append({"severity": "medium", "kind": "time_gaps", "columns": [t],
                                  "text": f"{int(expected) - int(present)} {g}{'s' if int(expected) - int(present) != 1 else ''} missing between {lo} and {hi} "
                                          f"(first: {gaps[0]})"})
                if incomplete:
                    risks.append({"severity": "medium", "kind": "incomplete_series", "columns": [t, *series_cols],
                                  "text": f"{incomplete:,} series by {', '.join(series_cols)} lack some {g}s that others have"})
                age = (date.today() - (hi if isinstance(hi, date) else date.fromisoformat(str(hi)[:10]))).days
                freshness = {"column": t, "latest": str(hi), "age_days": age, "grain": g, "refreshed_at": str(row["refreshed_at"])}
    for c in columns:
        if c["semantic_type"] == "personal_data":
            risks.append({"severity": "medium", "kind": "personal_data", "columns": [c["name"]],
                          "text": f"{c['name']} looks like personal data; consider a column policy"})
        for issue in c["issues"]:
            if "variants" in issue or "spaces" in issue:
                risks.append({"severity": "low", "kind": "inconsistent_values", "columns": [c["name"]], "text": f"{c['name']}: {issue}"})
    order = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda r: order[r["severity"]])
    summary_bits = [f"{rows:,} rows"]
    if grain:
        summary_bits.append("one row per " + " × ".join(grain))
    if freshness:
        summary_bits.append(f"data through {freshness['latest']}")
    if risks:
        summary_bits.append(f"{len(risks)} quality note{'s' if len(risks) != 1 else ''}, the first: {risks[0]['text']}")
    else:
        summary_bits.append("no quality risks found")
    return {"dataset": row["name"], "version": row["version"], "rows": rows, "grain": grain, "keys": keys,
            "duplicates_at_grain": dup_at_grain, "columns": columns, "missing_together": together, "continuity": continuity,
            "freshness": freshness, "risks": risks, "summary": "; ".join(summary_bits) + "."}


def _time_grain(cur, table: str, col: str) -> str:
    """The grain the dates are recorded at, read from the dates themselves: all first-of-month dates are monthly
    (quarterly or yearly when only those months occur), one weekday is weekly, anything else daily."""
    c = f"CAST({_q(col)} AS DATE)"
    not_first, months, weekdays = cur.execute(
        f"SELECT count(*) FILTER (WHERE day({c}) <> 1), list(DISTINCT month({c})), count(DISTINCT dayofweek({c})) "
        f"FROM {table} WHERE {_q(col)} IS NOT NULL").fetchone()
    if not not_first:
        months = set(months or [])
        if months <= {1}:
            return "year"
        if months <= {1, 4, 7, 10} and len(months) > 1:
            return "quarter"
        return "month"
    return "week" if weekdays == 1 else "day"


def _relationships(db, current: list[dict[str, Any]], per: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    info = {d["name"]: {c["name"]: c for c in per[d["name"]]["columns"]} for d in current}
    physical = {d["name"]: f'data.{_q(d["physical_table"])}' for d in current}
    with db.read() as cur:
        for a, b in itertools.combinations(sorted(info), 2):
            shared = sorted(set(info[a]) & set(info[b]))
            for col in shared:
                ca, cb = info[a][col], info[b][col]
                if ca["role"] == "measure" or cb["role"] == "measure":
                    continue
                fa, fb = _family(ca["type"]), _family(cb["type"])
                if fa != fb:
                    out.append({"kind": "inconsistent_definition", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col,
                                "evidence": {"types": [ca["type"], cb["type"]]},
                                "text": f"{col} is {ca['type']} in {a} but {cb['type']} in {b}; joins on it will not match without a cast"})
                    continue
                va, vb, both = cur.execute(
                    f"WITH x AS (SELECT DISTINCT CAST({_q(col)} AS VARCHAR) AS v FROM {physical[a]} WHERE {_q(col)} IS NOT NULL), "
                    f"y AS (SELECT DISTINCT CAST({_q(col)} AS VARCHAR) AS v FROM {physical[b]} WHERE {_q(col)} IS NOT NULL) "
                    "SELECT (SELECT count(*) FROM x), (SELECT count(*) FROM y), (SELECT count(*) FROM x JOIN y USING (v))").fetchone()
                if not va or not vb:
                    continue
                folded = cur.execute(
                    f"WITH x AS (SELECT DISTINCT lower(trim(CAST({_q(col)} AS VARCHAR))) AS v FROM {physical[a]}), "
                    f"y AS (SELECT DISTINCT lower(trim(CAST({_q(col)} AS VARCHAR))) AS v FROM {physical[b]}) "
                    "SELECT count(*) FROM x JOIN y USING (v)").fetchone()[0] if fa == "text" else both
                ev = {"distinct": [va, vb], "matching": both, "containment": [round(both / va, 3), round(both / vb, 3)]}
                if both == 0 and folded == 0:
                    out.append({"kind": "inconsistent_definition", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col,
                                "evidence": ev, "text": f"{col} in {a} and {col} in {b} share no values; they likely mean different things"})
                    continue
                if folded > both * 1.2 + 0:
                    out.append({"kind": "inconsistent_definition", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col,
                                "evidence": {**ev, "matching_ignoring_case": folded},
                                "text": f"{col} values differ only by case or spacing between {a} and {b} ({folded} match when folded, {both} exactly)"})
                key_b, key_a = col in per[b].get("keys", []), col in per[a].get("keys", [])
                if key_b and both / va >= 0.98:
                    out.append({"kind": "foreign_key", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col, "evidence": ev,
                                "text": f"{a}.{col} references {b}.{col} ({both / va:.0%} of values found)"})
                elif key_a and both / vb >= 0.98:
                    out.append({"kind": "foreign_key", "from_dataset": b, "from_column": col, "to_dataset": a, "to_column": col, "evidence": ev,
                                "text": f"{b}.{col} references {a}.{col} ({both / vb:.0%} of values found)"})
                elif both / min(va, vb) >= 0.5:
                    out.append({"kind": "shared_dimension", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col, "evidence": ev,
                                "text": f"{a} and {b} share {col} ({both} common values); they can be compared on it"})
                elif both / min(va, vb) < 0.5:
                    out.append({"kind": "inconsistent_definition", "from_dataset": a, "from_column": col, "to_dataset": b, "to_column": col,
                                "evidence": ev, "text": f"{col} overlaps only {both / min(va, vb):.0%} between {a} and {b}"})
            # overlapping sources: the same grain and the same measures, reconciled
            ga, gb = per[a].get("grain") or [], per[b].get("grain") or []
            grain_a = ga or [c for c in info[a] if info[a][c]["role"] in NON_MEASURE]
            grain_b = gb or [c for c in info[b] if info[b][c]["role"] in NON_MEASURE]
            measures = sorted(c for c in shared if info[a][c]["role"] == "measure" and info[b][c]["role"] == "measure")
            if measures and set(grain_a) == set(grain_b) and grain_a:
                totals = {}
                for m in measures[:6]:
                    sa, sb = cur.execute(f"SELECT (SELECT sum(CAST({_q(m)} AS DOUBLE)) FROM {physical[a]}), "
                                         f"(SELECT sum(CAST({_q(m)} AS DOUBLE)) FROM {physical[b]})").fetchone()
                    totals[m] = {a: sa, b: sb, "difference": (sb or 0) - (sa or 0),
                                 "relative": round(((sb or 0) - (sa or 0)) / sa, 6) if sa else None}
                differing = [m for m, t in totals.items() if t["relative"] not in (None, 0.0) and abs(t["relative"]) > 1e-9]
                text = (f"{a} and {b} carry the same measures ({', '.join(measures[:6])}) at the same grain ({' × '.join(sorted(grain_a))})"
                        + (f"; totals differ on {', '.join(differing)} (for example {differing[0]}: {totals[differing[0]]['relative']:+.2%})"
                           if differing else "; totals agree"))
                out.append({"kind": "overlap", "from_dataset": a, "from_column": None, "to_dataset": b, "to_column": None,
                            "evidence": {"grain": sorted(grain_a), "measures": measures, "totals": totals, "differing": differing}, "text": text})
    return out


def impact_of(db, semantic, actor: Principal, ws: str, dataset: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
    cols = set(columns or [])
    out: list[dict[str, Any]] = []
    affected_metrics: list[str] = []
    from .semantic import MetricSpec  # noqa: PLC0415

    for r in db.query("SELECT m.name, m.status, v.spec FROM metrics m JOIN metric_versions v ON v.name = m.name AND v.version = m.current_version"):
        spec = MetricSpec.model_validate_json(r["spec"])
        if spec.dataset != dataset:
            continue
        reads = spec.columns()
        if cols and not reads & cols:
            continue
        affected_metrics.append(spec.name)
        why = f"reads {', '.join(sorted(reads & cols))}" if cols else f"computed from {dataset}"
        out.append({"kind": "metric", "name": spec.name, "title": spec.title, "status": r["status"], "why": why, "metrics": [spec.name]})
    for w in db.query("SELECT id, version, title, deps, status FROM workflows WHERE status IN ('active', 'stale') "
                      "QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1"):
        deps = json.loads(w["deps"])
        ms = [m for m in deps.get("metrics", {}) if m in affected_metrics]
        if dataset in deps.get("datasets", {}) and (not cols or ms) or ms:
            out.append({"kind": "workflow", "name": w["id"], "title": w["title"], "status": w["status"], "metrics": ms,
                        "why": "uses " + (", ".join(ms) if ms else dataset)})
    for b in db.query("SELECT id, title, design, status FROM dashboards WHERE status <> 'retired'"):
        ms = [m["name"] for m in json.loads(b["design"]).get("metrics", []) if m["name"] in affected_metrics]
        if ms:
            out.append({"kind": "dashboard", "name": b["id"], "title": b["title"], "status": b["status"], "metrics": ms,
                        "why": "shows " + ", ".join(ms)})
    for m in db.query("SELECT id, title, metric, enabled FROM monitors"):
        if m["metric"] in affected_metrics:
            out.append({"kind": "monitor", "name": m["id"], "title": m["title"], "status": "enabled" if m["enabled"] else "disabled",
                        "metrics": [m["metric"]], "why": f"watches {m['metric']}"})
    for a in db.query("SELECT r.from_name AS id, e.description AS question FROM relations r LEFT JOIN entities e "
                      "ON e.kind = 'analysis' AND e.name = r.from_name WHERE r.from_kind = 'analysis' AND r.rel = 'read' "
                      "AND r.to_kind = 'dataset' AND r.to_name = ? ORDER BY r.created_at DESC LIMIT 50", [dataset]):
        metric = db.scalar("SELECT to_name FROM relations WHERE from_kind = 'analysis' AND from_name = ? AND rel = 'used'", [a["id"]])
        if cols and metric not in affected_metrics:
            continue
        out.append({"kind": "analysis", "name": a["id"], "title": (a["question"] or a["id"])[:120], "metric": metric,
                    "metrics": [metric] if metric else [], "why": f"read {dataset}"})
        for art in db.query("SELECT from_name FROM relations WHERE from_kind = 'artifact' AND rel = 'produced_from' AND to_name = ?", [a["id"]]):
            out.append({"kind": "artifact", "name": art["from_name"], "title": art["from_name"], "metrics": [metric] if metric else [],
                        "why": f"produced from analysis {a['id']}"})
    return out


__all__ = ["CatalogService", "impact_of", "semantic_type", "tokens"]
