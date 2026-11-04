# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Domain knowledge packages: versioned starting points for an industry or business function.

A package (``packages/<id>.json``) holds the vocabulary of a business area: the signals that suggest it (words that
appear in dataset and column names), its main concepts, candidate metrics described by the column names they are
usually built from, example questions, the sources an organization in that area usually has, and glossary terms.

Packages are proposals, never authority. Recognition reads metadata only (dataset names, titles, descriptions,
column names and the catalog's semantic types), never rows, and what it finds is offered to an administrator,
who approves it into the workspace's governed metrics and glossary (see ``onboarding.py``).
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field

_SPLIT = re.compile(r"[^a-z0-9]+")


class Concept(BaseModel):
    name: str
    synonyms: list[str]


class CandidateMetric(BaseModel):
    name: str
    title: str
    agg: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    columns: list[str]                   # column names the metric is usually built from, best first
    format: Literal["number", "currency", "percent"] = "number"
    higher_is_better: bool = True
    description: str = ""


class SourceHint(BaseModel):
    name: str
    signals: list[str]


class Term(BaseModel):
    term: str
    definition: str
    metric: str | None = None


class DomainPackage(BaseModel):
    id: str
    version: str
    title: str
    summary: str
    signals: list[str]
    concepts: list[Concept] = Field(default_factory=list)
    metrics: list[CandidateMetric] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    sources: list[SourceHint] = Field(default_factory=list)
    glossary: list[Term] = Field(default_factory=list)
    notes: str = ""


@cache
def packages() -> dict[str, DomainPackage]:
    out: dict[str, DomainPackage] = {}
    for f in sorted(resources.files(__package__).joinpath("packages").iterdir(), key=lambda p: p.name):
        if f.name.endswith(".json"):
            p = DomainPackage(**json.loads(f.read_text()))
            out[p.id] = p
    return out


def get(package_id: str) -> DomainPackage:
    try:
        return packages()[package_id]
    except KeyError:
        raise KeyError(f"unknown domain package {package_id!r}") from None


def words(text: str | None) -> set[str]:
    """Lower-case words of a name or title, adjacent pairs joined by ``_``, and each name whole
    (``lead_time_days`` gives lead, time, days, lead_time, time_days and lead_time_days)."""
    if not text:
        return set()
    low = text.lower()
    seq = [w for w in _SPLIT.split(low) if w]
    pairs = {f"{a}_{b}" for a, b in zip(seq, seq[1:], strict=False)}
    whole = {w for w in re.split(r"[^a-z0-9_]+", low) if w}
    return set(seq) | pairs | whole


def recognize(datasets: list[dict[str, Any]], documents: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Rank packages by how many of their signals the workspace's metadata shows.

    ``datasets``: [{name, title, description, columns: [{name, semantic_type}]}]. ``documents``: [{title}].
    Each result lists the evidence (which dataset or column matched which signal), the concepts seen, and the
    usual sources the workspace does not appear to have yet. Strength is ``strong`` with five or more distinct
    signals across at least two places, ``possible`` with three or more, otherwise the package is left out."""
    places: list[tuple[str, set[str]]] = []
    for d in datasets:
        places.append((d["name"], words(d["name"]) | words(d.get("title")) | words(d.get("description"))))
        for c in d.get("columns", []):
            places.append((f"{d['name']}.{c['name']}", words(c["name"])))
    for doc in documents or []:
        places.append((f"document:{doc['title']}", words(doc["title"])))
    all_words = set().union(*(w for _, w in places)) if places else set()
    ranked = []
    for p in packages().values():
        signals = set(p.signals)
        evidence: dict[str, list[str]] = {}
        for where, ws in places:
            hit = sorted(signals & ws)
            if hit:
                evidence[where] = hit
        matched = set().union(*evidence.values()) if evidence else set()
        if len(matched) < 3:
            continue
        strength = "strong" if len(matched) >= 5 and len(evidence) >= 2 else "possible"
        concepts = [c.name for c in p.concepts if set(c.synonyms) & all_words]
        missing = [s.name for s in p.sources if not set(s.signals) & all_words]
        ranked.append({"id": p.id, "version": p.version, "title": p.title, "summary": p.summary, "strength": strength,
                       "score": len(matched), "signals": sorted(matched), "concepts": concepts,
                       "evidence": [{"where": k, "signals": v} for k, v in list(evidence.items())[:12]],
                       "missing_sources": missing, "notes": p.notes})
    ranked.sort(key=lambda r: (-r["score"], r["id"]))
    return ranked


_NUMERIC_TYPES = {"amount", "quantity", "count", "percent"}


def match_metric(m: CandidateMetric, datasets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The best dataset column for a candidate metric, judged from names and semantic types only.

    Sums and averages need a numeric column; distinct counts need an identifier. The time column is the dataset's
    date column (the one the catalog measured continuity on when there is one), and dimensions are its small
    categorical columns."""
    for wanted in m.columns:
        for d in datasets:
            for c in d.get("columns", []):
                if c["name"].lower() != wanted:
                    continue
                st = c.get("semantic_type")
                if m.agg in ("sum", "avg", "min", "max") and st not in _NUMERIC_TYPES:
                    continue
                if m.agg == "count_distinct" and st not in ("identifier", "category"):
                    continue
                dates = [x["name"] for x in d["columns"] if x.get("semantic_type") == "date"]
                time_col = d.get("time_column") if d.get("time_column") in dates else (dates[0] if dates else None)
                dims = [x["name"] for x in d["columns"] if x.get("semantic_type") in ("category", "geography")
                        and (x.get("distinct") or 0) <= 200][:6]
                return {"dataset": d["name"], "column": c["name"], "time_column": time_col, "dimensions": dims,
                        "semantic_type": st}
    return None
