# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Datasheets and annotation guideline drafts: write down what a dataset is, so the knowledge outlives the person who
loaded it.

"Document the annotations dataset", "Write a data dictionary for pnl" and "Draft annotation guidelines" ask for the
write-up a data team keeps next to a dataset. Lyst assembles it from what it has measured, never from a guess:

* **Overview**: title, description, owner, classification, source file, versions, rows, the period it covers and how
  current it is (the catalog).
* **Data dictionary**: each column's type, role, how full it is, distinct values, examples and its access policy.
* **Quality**: open data quality issues by kind (the quality review) and the catalog's risks.
* **Lineage and use**: related datasets and the approved metrics built on it.
* **Labeling**, when the dataset is labeled: the label set, agreement and annotators (the label audit), and a draft
  guideline: each label with examples every annotator agreed on, an edge-case rule to write for each pair of labels
  annotators confuse (with the split items as examples), and formatting rules taken from the data's own problems.

Definitions are left for the team to write and marked as such: Lyst cannot know what a label is meant to mean, only
how it has been used. Everything is read as the asker; a column the asker may not see is left out and listed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from . import annotations as ann
from . import brief as brief_mod
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(datasheets?|data ?(dictionary|card|sheet|documentation)|codebook|schema (doc|documentation)|"
    r"document (the |this |my |our )?[\w ]{0,40}?(dataset|data|table)s?|"
    r"(annotation|labell?ing) (guidelines?|guide|instructions|manual|rules|standards?|playbook)|"
    r"(write|draft|create|make) (the |a |an )?(annotation |labell?ing )?(guidelines?|codebook))\b", re.I)
_GUIDE = re.compile(r"\b(guidelines?|guide|instructions|manual|rules|standards?|playbook|codebook)\b", re.I)
EXAMPLES = 3


def asks_for_datasheet(text: str) -> bool:
    return bool(ASKS.search(text or ""))


def pick(datasets: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    """The dataset the question names (by name or title), the labeled one when guidelines are asked for, or the only
    one there is."""
    q = question.lower()
    named = [d for d in datasets if re.search(rf"\b{re.escape(d['name'].lower())}\b", q)
             or (d.get("title") and d["title"].lower() in q)]
    if named:
        return max(named, key=lambda d: len(d["name"]))
    if _GUIDE.search(question):
        found = ann.find_labeled(datasets, question)
        if found:
            return next(d for d in datasets if d["name"] == found["dataset"])
    return datasets[0] if len(datasets) == 1 else None


def understanding(text: str, dataset: str | None) -> dict[str, Any]:
    return {**brief_mod.understanding(text), "intent": "datasheet", "dataset": dataset,
            "guidelines": bool(re.search(r"\b(annotation|labell?ing)\b", text, re.I) and _GUIDE.search(text))}


def _examples(col: dict[str, Any]) -> str:
    top = col.get("top") or []
    return ", ".join(str(v) for v, _ in top[:3])


def _person(services: Any, user_id: Any) -> str | None:
    """The owner as a reader knows them: their name, else their email; None when it cannot be looked up."""
    db = getattr(getattr(services, "runtime", None), "db", None)
    if not user_id or db is None:
        return None
    try:
        rows = db.query("SELECT name, email FROM users WHERE id = ?", [user_id])
    except Exception:  # noqa: BLE001 - a missing directory leaves the id, which is still true
        return None
    return (rows[0].get("name") or rows[0].get("email")) if rows else None


@capability(REGISTRY, "datasheet", agent="communications", phase="deliver", permission="data.query",
            description="A datasheet for one dataset from what Lyst has measured: overview, data dictionary, quality, "
                        "lineage and use, and for labeled data the label set, agreement and a draft annotation guideline.")
def datasheet(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    s = ctx.services
    u = ctx.outputs["understand"]
    overview = {d["name"]: d for d in s.catalog.overview(ctx.actor, ctx.workspace_id)}
    if not u.get("dataset") or u["dataset"] not in overview:
        return {"empty": True, "datasets": sorted(overview)}
    ds = u["dataset"]
    ov = overview[ds]
    ins = s.catalog.insights(ctx.actor, ctx.workspace_id, ds)
    out: dict[str, Any] = {"dataset": ds, "overview": ov, "columns": ins.get("columns", []),
                           "relationships": ins.get("relationships", []), "risks": ins.get("risks"),
                           "freshness": ins.get("freshness") or ov.get("freshness"), "contract": ins.get("contract"),
                           "evidence": []}
    try:
        full = next(x for x in s.data.catalog(ctx.actor, ctx.workspace_id) if x["name"] == ds)
        seen = {c["name"] for c in out["columns"]}
        out["hidden"] = sum(c["name"] not in seen for c in full.get("schema", []))
    except Exception:  # noqa: BLE001 - the catalog view is what the asker may see; nothing more to count
        out["hidden"] = 0
    out["owner_name"] = _person(s, ov.get("owner"))
    out["metrics"] = [m.title for m in s.semantic.metrics(ctx.actor, ctx.workspace_id) if m.dataset == ds]
    quality = getattr(s.runtime, "quality", None) if s.runtime is not None else None
    if quality is not None:
        try:
            rep = quality.report(ctx.actor, ctx.workspace_id, ds)
            out["quality"] = {"counts": rep["counts"], "open": rep["open"], "fixable": rep["fixable"]}
        except Exception:  # noqa: BLE001 - too large to review row by row, or not readable: say so instead
            out["quality"] = None
    found = ann.find_labeled([{"name": ds, "title": ov.get("title"), "description": ov.get("description"),
                               "columns": out["columns"]}], u["question"])
    if found:
        a = ann.audit(ctx, found)
        out["labels"] = a
        out["evidence"] += a.get("evidence", [])
        out["guide_examples"] = _guide_examples(ctx, found, a)
    return out


def _guide_examples(ctx: Context, found: dict[str, Any], a: dict[str, Any]) -> dict[str, list[str]]:
    """Up to three items per label that every annotator agreed on, by their text when there is a text column."""
    if not a.get("annotated_by_several"):
        return {}
    split = {x["item"] for x in a.get("split", [])}
    from ..semantic import _q  # noqa: PLC0415

    show = found["text"] or found["item"]
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id,
                                f"SELECT {_q(found['item'])}, {_q(show)}, {_q(found['label'])} FROM {_q(found['dataset'])}",
                                task_id=ctx.task_id)
    canon = {lab["norm"]: lab["label"] for lab in a["labels"]}
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for item, shown, label in r.rows:
        norm = ann.normalize(label)
        item = str(item)
        if norm is None or item in split or item in seen:
            continue
        lab = canon.get(norm, norm)
        if len(out.setdefault(lab, [])) < EXAMPLES:
            out[lab].append(f"{item}: {str(shown)[:100]}" if found["text"] else item)
            seen.add(item)
    return out


def _guideline(a: dict[str, Any], examples: dict[str, list[str]], found: dict[str, Any]) -> list[str]:
    lines = ["Draft from the data. Definitions are for the team to write; the examples and rules come from how the "
             "labels have been used."]
    for lab in a["labels"]:
        lines.append(f"Label \"{lab['label']}\" ({lab['share']:.0%} of labels). Definition: to be written by the team."
                     + (f" Examples every annotator agreed on: {'; '.join(examples.get(lab['label'], []))}."
                        if examples.get(lab["label"]) else ""))
    split = a.get("split", [])
    for c in a.get("confused", [])[:3]:
        cases = ([x["item"] for x in split if set(x["votes"]) == {c["a"], c["b"]}]
                 or [x["item"] for x in split if c["a"] in x["votes"] and c["b"] in x["votes"]])[:3]
        lines.append(f"Edge case: when an item could be \"{c['a']}\" or \"{c['b']}\", decide by a rule the team agrees "
                     f"({c['pairs']:,} disagreements so far)" + (f"; settle it on {', '.join(cases)}." if cases else "."))
    spell = ", ".join(f"\"{lab['label']}\"" for lab in a["labels"])
    lines.append(f"Use exactly these spellings: {spell}.")
    if a.get("missing"):
        lines.append(f"Never leave {found['label']} empty; skip the item instead ({a['missing']:,} empty labels so far).")
    if a.get("repeated"):
        lines.append("Label each item once; to change a label, edit it rather than adding a second one.")
    for r in a.get("rare", [])[:2]:
        lines.append(f"\"{r['label']}\" is rare ({r['share']:.1%}): flag uncertain cases for review rather than "
                     "defaulting to a common label.")
    return lines


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, d = o["understand"], o["datasheet"]
    if d.get("empty"):
        names = ", ".join(d.get("datasets") or []) or "none yet"
        answer = f"Which dataset should Lyst document? Name one: {names}."
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="needs_input")
        out["mode"] = "datasheet"
        return out
    ds, ov = d["dataset"], d["overview"]
    fresh = d.get("freshness") or {}
    cols = d["columns"]
    q = d.get("quality")
    a = d.get("labels")
    guide = u.get("guidelines") and a
    parts = [f"{ov.get('title') or ds} ({ds}): {int(ov.get('rows') or 0):,} rows, {len(cols)} columns, version "
             f"{ov.get('version')}" + (f", data through {fresh['latest']}" if fresh.get("latest") else "")]
    if q is not None:
        parts.append(f"{q['open']:,} open data quality issue{'s' if q['open'] != 1 else ''}" if q["open"] else
                     "no open data quality issues")
    if a and a.get("alpha") is not None:
        parts.append(f"{len(a['labels'])} labels with agreement (Krippendorff's alpha) {a['alpha']:.2f}")
    answer = ("Draft annotation guideline for " if guide else "Datasheet for ") + "; ".join(parts) + "."
    findings: list[str] = []
    if ov.get("description"):
        findings.append(f"What it is: {ov['description']}")
    findings.append(f"Owner {d.get('owner_name') or ov.get('owner') or 'not set'}, classification {ov.get('classification')}, loaded from "
                    f"{ov.get('source') or 'a connection'}, last refreshed {str(ov.get('refreshed_at') or '')[:10]}.")
    if ov.get("summary"):
        findings.append(f"Catalog notes: {ov['summary']}")
    if d["metrics"]:
        findings.append(f"Approved metrics built on it: {', '.join(d['metrics'])}.")
    rel = d.get("relationships") or []
    if rel:
        findings.append("Related datasets: " + ", ".join(
            f"{r.get('to_dataset') if r.get('from_dataset') == ds else r.get('from_dataset')} ({r.get('kind')})" for r in rel[:6]) + ".")
    if d.get("hidden"):
        findings.append(f"{d['hidden']} column{'s are' if d['hidden'] != 1 else ' is'} not documented: not visible with "
                        "your access.")
    tables = [{"title": f"Data dictionary, {ds}", "columns": ["column", "type", "role", "filled", "distinct", "examples", "notes"],
               "rows": [[c["name"], c.get("type"), c.get("semantic_type") or c.get("role"),
                         f"{1 - float(c.get('nulls') or 0):.0%}", c.get("distinct"), _examples(c),
                         "; ".join(c.get("issues") or [])] for c in cols]}]
    if q is not None and q["counts"]:
        tables.append({"title": "Open data quality issues", "columns": ["kind", "count"],
                       "rows": [[k, v] for k, v in q["counts"].items()]})
    actions = []
    if q and q["open"]:
        actions.append(f"Review the {q['open']:,} open data quality issues of {ds}; Lyst has a proposal for {q['fixable']:,}.")
    if not d.get("contract"):
        actions.append(f"Protect {ds} with a data contract, so a new version with missing columns or changed types is caught.")
    guideline: list[str] = []
    if a:
        tables.append({"title": "Labels", "columns": ["label", "judgments", "share", "spellings"],
                       "rows": [[lab["label"], lab["judgments"], round(lab["share"], 4), len(lab["spellings"])]
                                for lab in a["labels"]]})
        if a.get("annotators"):
            tables.append({"title": "Annotators", "columns": ["annotator", "judgments", "agreement with others"],
                           "rows": [[p["annotator"], p["judgments"], None if p["agreement"] is None else round(p["agreement"], 3)]
                                    for p in a["annotators"]]})
        guideline = _guideline(a, d.get("guide_examples") or {}, a["found"])
        tables.append({"title": "Draft annotation guideline", "columns": ["#", "rule"],
                       "rows": [[i + 1, line] for i, line in enumerate(guideline)]})
        if a.get("split"):
            actions.append(f"Adjudicate the {len(a['split']):,} split items and add the decisions to the guideline as examples.")
        actions.append("Have the team write each label's definition into the draft guideline, then share it with "
                       "annotators and engineers.")
    if guide:
        tables.insert(0, tables.pop())  # the guideline first when that is what was asked for
    checks = [{"name": "from_measurements", "passed": True, "severity": "error",
               "detail": "every figure comes from the catalog's profile, the quality review or a recorded query"}]
    if d.get("hidden"):
        checks.append({"name": "access_scope", "passed": True, "severity": "warning",
                       "detail": f"{d['hidden']} column(s) not documented: not visible with your access"})
    assumptions = ["Built from Lyst's own measurements of the current version; descriptions and label definitions are "
                   "the team's to write and are marked where missing.",
                   f"Written {datetime.now(UTC):%d %b %Y}; ask again after the data changes for a current datasheet."]
    out = brief_mod._shape(u["question"], u, answer, findings, [], actions, tables, [], checks,
                           {"evidence": d.get("evidence", [])}, status="complete", assumptions=assumptions)
    out["mode"] = "datasheet"
    out["supporting"]["plan"] = [{"capability": "datasheet", "agent": "communications"}]
    out["supporting"]["workflow"]["strategy"] = "datasheet"
    out["supporting"]["brief"] = None
    out["supporting"]["datasheet"] = {"dataset": ds, "guideline": guideline}
    return out


__all__ = ["ASKS", "asks_for_datasheet", "datasheet", "pick", "result", "understanding"]
