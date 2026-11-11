# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Label audit: how good are a dataset's annotations, where do annotators disagree, and what should change.

"How consistent are the annotators?", "Which items do annotators disagree on?" and "Check the label quality" are the
questions a data annotation or ML data team asks of a labeled dataset. Lyst finds the labeled dataset from its columns
(an item, a label and usually an annotator), reads it as the asker through the SQL guard, and reports:

1. **The labels**: each label's share, spellings that differ only by case, spaces or punctuation (``Positive `` and
   ``positive``), judgments with no label, and classes too rare to learn from.
2. **Agreement**: Krippendorff's alpha (nominal, any number of annotators per item, missing judgments allowed), the
   share of items every annotator agreed on, and the label pairs annotators confuse most, which is where a guideline
   needs a rule.
3. **Annotators**: each annotator's agreement with the others' majority on the items they share, and the label they
   use more than the rest of the team, so a drifting or biased annotator is named with the evidence.
4. **Items to adjudicate**: the items with a split vote, hardest first, and the same annotator labeling an item twice
   with different answers.

Labels are compared after normalizing case, spaces and punctuation, and the answer says so. Lyst only reports: it
does not relabel, merge spellings or remove judgments. Spelling fixes go through the data quality review, where every
change is staged and approved.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from ..semantic import _q
from . import brief as brief_mod
from .registry import REGISTRY, Context, capability

ASKS = re.compile(
    r"\b(annotators?|labell?ers?|(annotation|labell?ing) (quality|agreement|consistency|errors?|audit|review|noise)|"
    r"inter[- ]?(annotator|rater|coder)|krippendorff|kappa|raters?|adjudicat\w*|"
    r"gold (set|labels?|standard)|label (quality|noise|distribution|balance|consistency|errors?|spellings?)|"
    r"class (balance|imbalance)|(which|what) labels|labels? (are )?used|mislabel\w*)\b", re.I)

_ITEM = ["item_id", "item", "example_id", "sample_id", "record_id", "doc_id", "document_id", "text_id", "image_id",
         "utterance_id", "task_id", "question_id", "row_id", "id"]
_ANNOTATOR = ["annotator", "annotator_id", "labeler", "labeller", "labeler_id", "rater", "rater_id", "worker",
              "worker_id", "reviewer", "coder", "judge", "annotated_by", "labeled_by", "labelled_by"]
_LABEL = ["label", "labels", "annotation", "class", "category", "tag", "sentiment", "intent", "judgment", "judgement",
          "answer", "rating", "gold_label", "target"]
_CONFIDENCE = ["confidence", "certainty", "score", "label_confidence"]
_TEXT = ["text", "content", "sentence", "utterance", "body", "review", "comment", "description", "prompt"]

GOOD_ALPHA, TENTATIVE_ALPHA = 0.8, 0.667  # Krippendorff's usual thresholds for reliable and tentative conclusions
RARE_SHARE = 0.05
LISTED = 25


def asks_for_audit(text: str) -> bool:
    return bool(ASKS.search(text or "")) and not brief_mod.ELSEWHERE.search(text or "")


def understanding(text: str, found: dict[str, Any]) -> dict[str, Any]:
    return {**brief_mod.understanding(text), "intent": "annotations", "dataset": found["dataset"]}


def normalize(v: Any) -> str | None:
    """``"Positive "``, ``"POSITIVE"`` and ``"positive."`` are one label; an empty cell is no label."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = re.sub(r"[\s_\-]+", " ", str(v).strip().lower())
    s = re.sub(r"[^\w\s]", "", s).strip()
    return s or None


def find_labeled(datasets: list[dict[str, Any]], question: str = "") -> dict[str, Any] | None:
    """The dataset that looks labeled, from column names only: a label column and an item column, plus an annotator
    column when several people labeled each item. A dataset the question names wins."""
    best, best_score = None, -1
    q = question.lower()
    for d in datasets:
        names = {c["name"].lower(): c["name"] for c in d.get("columns", [])}

        def pick(options: list[str], _names: dict[str, str] = names) -> str | None:
            return next((_names[o] for o in options if o in _names), None)

        label, item = pick(_LABEL), pick(_ITEM)
        if not label or not item or label == item:
            continue
        found = {"dataset": d["name"], "title": d.get("title") or d["name"], "item": item, "label": label,
                 "annotator": pick(_ANNOTATOR), "confidence": pick(_CONFIDENCE), "text": pick(_TEXT)}
        text = f"{d['name']} {d.get('title') or ''} {d.get('description') or ''}".lower()
        score = 20 * (d["name"].lower() in q) + 5 * bool(found["annotator"]) + 3 * bool(re.search(r"annotat|label", text))
        if score > best_score:
            best, best_score = found, score
    return best


def metadata(ctx: Context) -> list[dict[str, Any]]:
    s = ctx.services
    out = []
    for d in s.catalog.overview(ctx.actor, ctx.workspace_id):
        try:
            ins = s.catalog.insights(ctx.actor, ctx.workspace_id, d["name"])
        except Exception:  # noqa: BLE001, S112 - a dataset the asker cannot inspect is not a candidate
            continue
        out.append({"name": d["name"], "title": d.get("title"), "description": d.get("description"),
                    "columns": ins.get("columns", [])})
    return out


def krippendorff_alpha(units: list[list[str]]) -> float | None:
    """Nominal Krippendorff's alpha from each item's labels (items with fewer than two labels carry no information).

    Built from the coincidence matrix: every ordered pair of labels within an item counts 1/(m-1), where m is the
    number of labels on that item. alpha = 1 - D_o / D_e."""
    pairs: Counter = Counter()
    for labels in units:
        m = len(labels)
        if m < 2:
            continue
        for a, b in itertools.permutations(range(m), 2):
            pairs[(labels[a], labels[b])] += 1.0 / (m - 1)
    n = sum(pairs.values())
    if n <= 1:
        return None
    totals: Counter = Counter()
    for (a, _), w in pairs.items():
        totals[a] += w
    observed = sum(w for (a, b), w in pairs.items() if a != b) / n
    expected = (n * n - sum(v * v for v in totals.values())) / (n * (n - 1))
    if expected <= 0:
        return 1.0 if observed == 0 else None
    return 1.0 - observed / expected


@capability(REGISTRY, "label_audit", agent="quality", phase="validate", permission="data.query",
            description="Label audit of an annotated dataset: label shares and spellings, missing labels, Krippendorff's "
                        "alpha, confused label pairs, each annotator's agreement and bias, and items to adjudicate.")
def label_audit(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:

    u = ctx.outputs["understand"]
    found = next((f for f in [find_labeled(metadata(ctx), u["question"])] if f and f["dataset"] == u.get("dataset")), None)
    if not found:
        return {"empty": True}
    return audit(ctx, found)


def audit(ctx: Context, found: dict[str, Any]) -> dict[str, Any]:
    """The audit itself, for a labeled dataset ``find_labeled`` found (the datasheet reuses it)."""
    from .engine import _evidence  # noqa: PLC0415

    cols = {k: found[k] for k in ("item", "annotator", "label", "confidence", "text") if found[k]}
    select = ", ".join(f"{_q(v)} AS {k}" for k, v in cols.items())
    r = ctx.services.data.query(ctx.actor, ctx.workspace_id, f"SELECT {select} FROM {_q(found['dataset'])}",
                                task_id=ctx.task_id)
    evidence = [_evidence(r, f"{found['dataset']}: every judgment")]
    df = pd.DataFrame(r.rows, columns=list(cols))
    df["item"] = df["item"].astype(str)
    df["raw"] = df["label"].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
    df["norm"] = df["label"].map(normalize)
    judged = df[df["norm"].notna()]

    # labels: shares, spellings, missing, rare classes
    labels = []
    shares = judged["norm"].value_counts()
    for lab, n in shares.items():
        spellings = judged.loc[judged["norm"] == lab, "raw"].value_counts()
        labels.append({"label": spellings.index[0], "norm": lab, "judgments": int(n), "share": float(n / len(judged)),
                       "spellings": {k: int(v) for k, v in spellings.items()}})
    variants = [lab for lab in labels if len(lab["spellings"]) > 1]
    rare = [lab for lab in labels if lab["share"] < RARE_SHARE]
    canon = {lab["norm"]: lab["label"] for lab in labels}

    out: dict[str, Any] = {"found": found, "rows": len(df), "judgments": len(judged), "missing": int(df["norm"].isna().sum()),
                           "items": int(df["item"].nunique()), "labels": labels, "variants": variants, "rare": rare,
                           "evidence": evidence}
    # repeated judgments: the same annotator on the same item more than once
    if "annotator" in df:
        df["annotator"] = df["annotator"].astype(str)
        judged = judged.assign(annotator=judged["annotator"].astype(str))
        rep = judged.groupby(["item", "annotator"])["norm"].agg(["count", "nunique"])
        out["repeated"] = int((rep["count"] > 1).sum())
        out["conflicting_repeats"] = [{"item": i, "annotator": a} for (i, a) in rep[rep["nunique"] > 1].index[:LISTED]]
        once = judged.drop_duplicates(["item", "annotator"], keep="last")
    else:
        out["repeated"], out["conflicting_repeats"] = 0, []
        once = judged
    per_item = once.groupby("item")["norm"].agg(list)
    multi = per_item[per_item.map(len) >= 2]
    out["annotated_by_several"] = len(multi)
    if len(multi):
        out["alpha"] = krippendorff_alpha(multi.tolist())
        unanimous = multi.map(lambda xs: len(set(xs)) == 1)
        out["unanimous_share"] = float(unanimous.mean())
        confused: Counter = Counter()
        for xs in multi:
            for a, b in itertools.combinations(xs, 2):
                if a != b:
                    confused[tuple(sorted((a, b)))] += 1
        out["confused"] = [{"a": canon.get(a, a), "b": canon.get(b, b), "pairs": int(n)} for (a, b), n in confused.most_common(5)]
        split = []
        texts = df.drop_duplicates("item").set_index("item")["text"] if "text" in df else None
        conf = once.groupby("item")["confidence"].median() if "confidence" in once else None
        for item, xs in multi[~unanimous].items():
            c = Counter(xs)
            top, k = c.most_common(1)[0]
            tied = list(c.values()).count(k) > 1
            split.append({"item": item, "judgments": len(xs), "votes": {canon.get(lab, lab): n for lab, n in c.most_common()},
                          "majority": None if tied else canon.get(top, top), "share": k / len(xs),
                          "text": None if texts is None else str(texts.get(item, ""))[:120],
                          "confidence": None if conf is None or pd.isna(conf.get(item)) else float(conf.get(item))})
        split.sort(key=lambda x: (x["share"], -x["judgments"]))
        out["split"] = split
        if conf is not None:
            agreed = conf[unanimous[unanimous].index.intersection(conf.index)]
            disagreed = conf[unanimous[~unanimous].index.intersection(conf.index)]
            out["confidence"] = {"agreed": float(agreed.median()) if len(agreed) else None,
                                 "split": float(disagreed.median()) if len(disagreed) else None}
    else:
        out["alpha"], out["unanimous_share"], out["confused"], out["split"] = None, None, [], []

    # annotators: agreement with the others' majority on shared items, and the label each over-uses
    people = []
    if "annotator" in once and len(multi):
        team = once["norm"].value_counts(normalize=True)
        by_item = once.groupby("item")
        for who, g in once.groupby("annotator"):
            agree = n = 0
            for item, lab in zip(g["item"], g["norm"], strict=False):
                others = [x for x in by_item.get_group(item).loc[lambda f, w=who: f["annotator"] != w, "norm"]]
                if not others:
                    continue
                c = Counter(others)
                top, k = c.most_common(1)[0]
                if list(c.values()).count(k) > 1:
                    continue  # the others are split themselves: no majority to agree with
                n += 1
                agree += lab == top
            mine = g["norm"].value_counts(normalize=True)
            rest = once.loc[once["annotator"] != who, "norm"].value_counts(normalize=True)
            gap = (mine - rest.reindex(mine.index).fillna(0)).sort_values(ascending=False)
            over = gap.index[0] if len(gap) else None
            people.append({"annotator": who, "judgments": len(g), "compared": n,
                           "agreement": agree / n if n else None,
                           "over_label": canon.get(over, over) if over is not None else None,
                           "over_share": float(mine.get(over, 0.0)) if over is not None else None,
                           "team_share": float(rest.get(over, team.get(over, 0.0))) if over is not None else None})
        rates = [p["agreement"] for p in people if p["agreement"] is not None]
        med = float(np.median(rates)) if rates else None
        for p in people:
            p["outlier"] = bool(med is not None and p["agreement"] is not None and len(rates) >= 3
                                and p["agreement"] < med - 0.1 and p["compared"] >= 10)
            p["biased"] = bool(p["over_share"] is not None and p["team_share"] is not None
                               and p["over_share"] - p["team_share"] >= 0.1 and p["judgments"] >= 20)
        people.sort(key=lambda p: (p["agreement"] if p["agreement"] is not None else 2))
        out["team_agreement"] = med
    out["annotators"] = people
    return out


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _alpha_words(a: float) -> str:
    if a >= GOOD_ALPHA:
        return "reliable"
    if a >= TENTATIVE_ALPHA:
        return "good enough only for tentative conclusions"
    return "too low to rely on"


def result(ctx: Context) -> dict[str, Any]:
    o = ctx.outputs
    u, a = o["understand"], o["annotations"]
    if a.get("empty"):
        answer = ("Lyst could not find a labeled dataset it can read here. It looks for a label column (label, class, "
                  "category, sentiment...) next to an item column (item_id, example_id...), and an annotator column when "
                  "several people labeled each item.")
        out = brief_mod._shape(u["question"], u, answer, [], [], [], [], [], [], {}, status="needs_input")
        out["mode"] = "annotations"
        return out
    f = a["found"]
    ds = f["dataset"]
    parts = [f"{ds}: {a['judgments']:,} labels on {a['items']:,} items, {len(a['labels'])} distinct labels"]
    if a.get("alpha") is not None:
        parts.append(f"agreement (Krippendorff's alpha) is {a['alpha']:.2f}, {_alpha_words(a['alpha'])}; annotators all "
                     f"agreed on {_pct(a['unanimous_share'])} of the {a['annotated_by_several']:,} items labeled more than once")
    answer = "; ".join(parts) + "."
    flags = []
    if a["split"]:
        flags.append(f"{len(a['split']):,} item{'s' if len(a['split']) != 1 else ''} need adjudication")
    odd = [p for p in a["annotators"] if p["outlier"] or p["biased"]]
    if odd:
        flags.append(f"{', '.join(p['annotator'] for p in odd)} label{'s' if len(odd) == 1 else ''} differently from the team")
    if a["variants"]:
        flags.append(f"{len(a['variants'])} label{'s are' if len(a['variants']) != 1 else ' is'} spelled more than one way")
    if a["missing"]:
        flags.append(f"{a['missing']:,} judgment{'s have' if a['missing'] != 1 else ' has'} no label")
    if flags:
        text = "; ".join(flags)
        answer += " " + text[0].upper() + text[1:] + "."
    if a["split"] and re.search(r"\b(disagree\w*|split|adjudicat\w*|conflict\w*)\b", u["question"], re.I):
        hardest = "; ".join(f"{x['item']} ({', '.join(f'{k} {v}' for k, v in x['votes'].items())})" for x in a["split"][:3])
        answer = (f"Annotators disagree on {len(a['split']):,} of the {a['annotated_by_several']:,} items labeled more than "
                  f"once; the hardest are {hardest}. " + answer)

    findings, actions, implications = [], [], []
    for lab in a["labels"]:
        sp = "" if len(lab["spellings"]) == 1 else " (spelled " + ", ".join(f"\"{k}\" {v}" for k, v in lab["spellings"].items()) + ")"
        findings.append(f"Label \"{lab['label']}\": {lab['judgments']:,} ({lab['share']:.0%}){sp}.")
    for c in a["confused"][:3]:
        findings.append(f"Most confused: \"{c['a']}\" and \"{c['b']}\" ({c['pairs']:,} disagreeing pairs).")
    for p in a["annotators"]:
        if p["agreement"] is None:
            continue
        line = f"Annotator {p['annotator']}: agrees with the others' majority on {_pct(p['agreement'])} of {p['compared']:,} items"
        if p["biased"]:
            line += f"; uses \"{p['over_label']}\" for {_pct(p['over_share'])} of labels against {_pct(p['team_share'])} for the rest"
        findings.append(line + ".")
    cf = a.get("confidence")
    if cf and cf.get("agreed") is not None and cf.get("split") is not None:
        findings.append(f"Median confidence is {cf['split']:.2f} on split items against {cf['agreed']:.2f} on unanimous ones.")
    if a["repeated"]:
        findings.append(f"{a['repeated']:,} item{'s were' if a['repeated'] != 1 else ' was'} labeled more than once by the same "
                        f"annotator ({len(a['conflicting_repeats'])} with different answers); the last label is used.")

    if a["split"]:
        actions.append(f"Adjudicate the {len(a['split']):,} split items, hardest first (table below), and keep the decided "
                       "label as gold.")
    if a["confused"]:
        c = a["confused"][0]
        actions.append(f"Add a guideline rule that separates \"{c['a']}\" from \"{c['b']}\", with two or three worked "
                       "examples from the split items.")
    for p in odd[:2]:
        why = (f"agrees with the team on {_pct(p['agreement'])} against a median of {_pct(a.get('team_agreement'))}"
               if p["outlier"] else f"uses \"{p['over_label']}\" {_pct(p['over_share'])} of the time against {_pct(p['team_share'])}")
        actions.append(f"Review a sample of {p['annotator']}'s labels with them: {why}.")
    if a["variants"]:
        v = a["variants"][0]
        actions.append(f"Merge the spellings of \"{v['label']}\" ({', '.join(repr(k) for k in v['spellings'])}) through "
                       f"the data quality review of {ds}, so they count as one label.")
    if a["missing"]:
        actions.append(f"Relabel or drop the {a['missing']:,} judgments with no label.")
    if a["rare"]:
        names = ", ".join(f"\"{r['label']}\" ({r['share']:.1%})" for r in a["rare"][:3])
        implications.append(f"{names} {'is' if len(a['rare']) == 1 else 'are'} rare; a model trained on this will see few "
                            "examples, so collect more or weight the class.")
    if a.get("alpha") is not None and a["alpha"] < TENTATIVE_ALPHA:
        implications.append("With agreement this low, a model's ceiling is the annotators' own consistency: fix the "
                            "guidelines before adding labels.")

    checks = [{"name": "numbers_backed", "passed": True, "severity": "error",
               "detail": f"every figure comes from the recorded query of {ds}"},
              {"name": "labels_consistent", "passed": not a["variants"], "severity": "warning",
               "detail": "each label has one spelling" if not a["variants"] else
               f"{len(a['variants'])} label(s) with more than one spelling"},
              {"name": "labels_complete", "passed": not a["missing"], "severity": "warning",
               "detail": "every judgment has a label" if not a["missing"] else f"{a['missing']:,} judgment(s) with no label"}]
    if a.get("alpha") is not None:
        checks.append({"name": "agreement", "passed": a["alpha"] >= TENTATIVE_ALPHA, "severity": "warning",
                       "detail": f"Krippendorff's alpha {a['alpha']:.3f} over {a['annotated_by_several']:,} items"})
    assumptions = [f"Labels are compared after normalizing case, spaces and punctuation in {f['label']}; items are "
                   f"{f['item']}" + (f" and annotators {f['annotator']}" if f["annotator"] else "") + ".",
                   "Agreement is Krippendorff's alpha for nominal labels: 1 is perfect, 0 is chance; 0.8 or more is "
                   "usually needed to rely on the labels, 0.667 for tentative conclusions.",
                   "Lyst does not relabel, merge spellings or remove judgments; changes go through the data quality review."]
    if not f["annotator"]:
        assumptions.append(f"{ds} has no annotator column, so agreement is measured between repeated labels of an item.")
    tables = [{"title": "Labels", "columns": ["label", "judgments", "share", "spellings"],
               "rows": [[lab["label"], lab["judgments"], round(lab["share"], 4), len(lab["spellings"])] for lab in a["labels"]]}]
    if a["annotators"]:
        tables.append({"title": "Annotators", "columns": ["annotator", "judgments", "agreement with others",
                                                          "most over-used label", "their share", "team share"],
                       "rows": [[p["annotator"], p["judgments"], None if p["agreement"] is None else round(p["agreement"], 3),
                                 p["over_label"], None if p["over_share"] is None else round(p["over_share"], 3),
                                 None if p["team_share"] is None else round(p["team_share"], 3)] for p in a["annotators"]]})
    if a["split"]:
        tables.append({"title": "Items to adjudicate, hardest first",
                       "columns": ["item", "votes", "majority", "agreement"] + (["text"] if f["text"] else []),
                       "rows": [[x["item"], ", ".join(f"{k} {v}" for k, v in x["votes"].items()), x["majority"] or "tie",
                                 round(x["share"], 3)] + ([x["text"]] if f["text"] else []) for x in a["split"][:LISTED]]})
    if a["confused"]:
        tables.append({"title": "Confused label pairs", "columns": ["label", "confused with", "disagreeing pairs"],
                       "rows": [[c["a"], c["b"], c["pairs"]] for c in a["confused"]]})
    charts = [{"type": "bar", "title": f"Labels in {ds}", "x": [lab["label"] for lab in a["labels"]],
               "y": [lab["judgments"] for lab in a["labels"]], "format": "number"}]
    status = "complete"
    out = brief_mod._shape(u["question"], u, answer, findings, implications, actions, tables, charts, checks,
                           {"evidence": a["evidence"]}, status=status, assumptions=assumptions)
    out["mode"] = "annotations"
    out["supporting"]["plan"] = [{"capability": "label_audit", "agent": "quality"}]
    out["supporting"]["workflow"]["strategy"] = "annotations"
    out["supporting"]["brief"] = None
    out["supporting"]["annotations"] = {k: a.get(k) for k in ("alpha", "unanimous_share", "team_agreement", "missing",
                                                              "repeated", "items", "judgments")}
    return out


__all__ = ["ASKS", "asks_for_audit", "audit", "find_labeled", "krippendorff_alpha", "label_audit", "normalize", "result",
           "understanding"]
