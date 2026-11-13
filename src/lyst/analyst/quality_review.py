# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Workspace data quality review: "Are there any data quality issues?" with no dataset named.

It used to stop and ask which dataset to profile. An analyst asked that question checks everything they can see, so Lyst
now runs the quality review (quality.py) over every dataset the asker may read and reports, worst first:

* each dataset's open issues by kind (duplicate rows, negative values, empty cells, inconsistent spellings, unusual
  values), how many Lyst has a proposed fix for, and fixes already waiting for approval;
* the datasets with nothing open.

The answer opens the review of the dataset with the most serious open issues. Lyst changes nothing: every fix still goes
through the quality review and an approval.
"""

from __future__ import annotations

from typing import Any

from . import brief as brief_mod
from .registry import REGISTRY, Context, capability

_SEV = {"duplicate": 0, "negative": 0, "null": 1, "variant": 1, "outlier": 2}
_PLURAL = {"duplicate": ("duplicate row", "duplicate rows"), "negative": ("negative value", "negative values"),
           "null": ("empty cell", "empty cells"), "variant": ("inconsistent spelling", "inconsistent spellings"),
           "outlier": ("unusual value", "unusual values")}


def understanding(text: str) -> dict[str, Any]:
    return {**brief_mod.understanding(text), "intent": "quality_review"}


def _kinds(counts: dict[str, int]) -> str:
    order = sorted(counts.items(), key=lambda kv: (_SEV.get(kv[0], 3), -kv[1]))
    return ", ".join(f"{n:,} {_PLURAL.get(k, (k, k))[n != 1]}" for k, n in order if n)


@capability(REGISTRY, "quality_review", agent="quality", phase="validate", permission="data.query",
            description="Data quality review across every dataset the asker may read: open issues by kind, proposed "
                        "fixes and fixes waiting for approval, worst dataset first.")
def quality_review(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    s = ctx.services
    quality = getattr(s.runtime, "quality", None) if s.runtime is not None else None
    rows, skipped = [], []
    for d in s.data.catalog(ctx.actor, ctx.workspace_id):
        if quality is None:
            skipped.append(d["name"])
            continue
        try:
            rep = quality.report(ctx.actor, ctx.workspace_id, d["name"])
        except Exception:  # noqa: BLE001 - one unreadable dataset is reported as not checked, not a failed review
            skipped.append(d["name"])
            continue
        kinds: dict[str, int] = {}
        for i in rep["issues"]:
            if i["status"] == "open":
                kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        rows.append({"dataset": d["name"], "rows": rep["rows"], "open": rep["open"], "fixable": rep["fixable"],
                     "valid": rep["valid"], "pending": len(rep["pending"]), "kinds": kinds,
                     "high": sum(n for k, n in kinds.items() if _SEV.get(k) == 0)})
    rows.sort(key=lambda r: (-r["high"], -r["open"], r["dataset"]))
    return {"datasets": rows, "not_checked": skipped}


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, q = o["understand"], o["quality_review"]
    rows, skipped = q["datasets"], q["not_checked"]
    if not rows:
        answer = ("There are no datasets you can read in this workspace yet, so there is nothing to check." if not skipped
                  else f"Lyst could not check {', '.join(skipped)}.")
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="needs_input")
        out["mode"] = "quality_review"
        return out
    flagged = [r for r in rows if r["open"]]
    clean = [r for r in rows if not r["open"]]
    pending = sum(r["pending"] for r in rows)
    findings, implications, actions = [], [], []
    opens = None
    n = len(rows)
    total = sum(r["open"] for r in flagged)
    if flagged:
        worst = flagged[0]
        answer = (f"Lyst checked {n} dataset{'s' if n != 1 else ''} and found {total:,} open data quality "
                  f"issue{'s' if total != 1 else ''} in {len(flagged)}. The most serious are in {worst['dataset']}: "
                  f"{_kinds(worst['kinds'])}.")
        if len(flagged) > 1:
            rest = flagged[1:4]
            answer += (" Also: " + "; ".join(f"{r['dataset']}, {r['open']:,}" for r in rest)
                       + (f" and {len(flagged) - 4} more" if len(flagged) > 4 else "") + ".")
        if clean:
            answer += f" {len(clean)} {'has' if len(clean) == 1 else 'have'} nothing open."
        opens = {"kind": "quality", "id": worst["dataset"], "title": f"Flagged rows in {worst['dataset']}"}
        implications.append("Metrics built on the affected columns may be misstated until the issues are reviewed.")
        for r in flagged[:3]:
            fix = f"; Lyst has a proposed fix for {r['fixable']:,}" if r["fixable"] else ""
            actions.append(f"Review the {r['open']:,} open issue{'s' if r['open'] != 1 else ''} in {r['dataset']}{fix}. "
                           "Nothing changes until a fix is approved.")
    else:
        answer = f"No open data quality issues in the {n} dataset{'s' if n != 1 else ''} Lyst checked."
    if pending:
        findings.append(f"{pending} data fix{'es' if pending != 1 else ''} already wait{'s' if pending == 1 else ''} "
                        "for approval.")
        actions.append("Decide the data fixes waiting under Approvals.")
    for r in rows:
        accepted = f"; {r['valid']:,} accepted as valid" if r["valid"] else ""
        if r["open"]:
            findings.append(f"{r['dataset']}: {r['open']:,} open in {r['rows']:,} rows ({_kinds(r['kinds'])}){accepted}.")
        else:
            findings.append(f"{r['dataset']}: nothing open in {r['rows']:,} rows{accepted}.")
    if skipped:
        findings.append(f"Not checked: {', '.join(skipped)}.")
    tables = [{"title": "Data quality by dataset",
               "columns": ["dataset", "rows", "open issues", "with a proposed fix", "waiting for approval", "kinds"],
               "rows": [[r["dataset"], r["rows"], r["open"], r["fixable"], r["pending"], _kinds(r["kinds"]) or "none"]
                        for r in rows]}]
    checks = [{"name": "every_dataset_checked", "passed": not skipped,
               "detail": "every dataset you can read was scanned" if not skipped else f"not checked: {', '.join(skipped)}"},
              {"name": "counts_from_review", "passed": True,
               "detail": "counts are the quality review's own, with issues accepted as valid left out"}]
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, [], checks, {},
                           status="complete",
                           assumptions=["Each dataset's current version is scanned with the same checks as its quality "
                                        "review. Lyst changes nothing; every fix goes through the review and an approval.",
                                        "Issues someone accepted as valid are not counted as open."])
    out["mode"] = "quality_review"
    out["open"] = opens
    out["supporting"]["plan"] = [{"capability": "quality_review", "agent": "quality"}]
    out["supporting"]["workflow"]["strategy"] = "quality_review"
    out["supporting"]["brief"] = None
    out["supporting"]["reproduce"] = "Open each dataset's quality review under Data; it runs the same scan."
    return out


__all__ = ["quality_review", "result", "understanding"]
