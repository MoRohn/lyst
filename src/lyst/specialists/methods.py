# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Specialist methods on workspace datasets: choose a method for a routed request, infer its columns, run it.

When a request routed through the Universal Analyst Core asks for work no governed metric covers (segmenting
customers, mining an order-to-cash event log, distances from stores to warehouses, reconciling two ledgers, finding
bursts in login events, mapping a network of links, predicting a two-valued outcome), the engine no longer stops at
"no approved metric matches". ``choose`` looks for a dataset the request names (every distinctive word of its name),
infers the columns the method needs from their names and types, and plans the registered ``run_method``
capability. If nothing fits, nothing is planned and the honest "no metric" answer stands.

Every read goes through ``DataService.query``: the SQL guard, row and column policies, governance access checks and
the audit trail all apply exactly as for any other query, under the requesting person's own permissions. A method
never sees data the person could not query directly.
"""

from __future__ import annotations

import re
from typing import Any

from ..analyst.registry import REGISTRY, Context, capability
from ..analytics import methods as M
from .router import tokens

MAX_ROWS = 200_000

_ID = re.compile(r"(^id$|_id$|^key$|_key$|^code$|_code$)", re.I)
_TIME = re.compile(r"(time|timestamp|date|_at$|^at_|datetime|occurred|created)", re.I)
_NUMERIC_TYPES = ("INT", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "NUMERIC", "BIGINT", "HUGEINT", "SMALLINT", "TINYINT")


def _cols(ds: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in ds.get("schema") or [] if isinstance(c, dict) and c.get("name")]


def _numeric(c: dict[str, Any]) -> bool:
    return any(t in str(c.get("type", "")).upper() for t in _NUMERIC_TYPES)


def _find(cols: list[dict[str, Any]], pattern: str, *, numeric: bool | None = None, exclude: set[str] | None = None) -> str | None:
    rx = re.compile(pattern, re.I)
    for c in cols:
        if c["name"] in (exclude or set()):
            continue
        if numeric is not None and _numeric(c) != numeric:
            continue
        if rx.search(c["name"]):
            return c["name"]
    return None


def _time_col(cols) -> str | None:
    for c in cols:
        if "TIMESTAMP" in str(c.get("type", "")).upper() or "DATE" in str(c.get("type", "")).upper():
            return c["name"]
    return _find(cols, _TIME.pattern)


# -- per-method column inference: dataset(s) -> params, or None when the data does not fit -------------------------


def _fit_process(ds, text):
    cols = _cols(ds)
    ts = _time_col(cols)
    case = _find(cols, r"(case|order|ticket|incident|request|instance|invoice|claim|application)_?(id|number|no)?$", exclude={ts})
    act = _find(cols, r"(activity|step|stage|event|status|task|action)$", exclude={ts, case})
    return {"case": case, "activity": act, "ts": ts} if ts and case and act else None


def _fit_log(ds, text):
    cols = _cols(ds)
    raw = _find(cols, r"^(line|raw|raw_line|log|log_line|message|entry)$", numeric=False)
    if raw and len(cols) <= 3:  # a dataset of raw log lines: parsed first (JSON, key=value, web access, syslog)
        return {"raw": raw}
    ts = _time_col(cols)
    ev = _find(cols, r"(event|action|event_type|type|activity|outcome|result)$", numeric=False, exclude={ts})
    ent = _find(cols, r"(user|account|host|ip|device|entity|source|username|principal|service|src)(_?(id|name|address|ip))?$",
                exclude={ts, ev})
    return {"ts": ts, "event": ev, "entity": ent} if ts and ev else None


def _fit_graph(ds, text):
    cols = _cols(ds)
    src = _find(cols, r"^(source|src|from|parent|caller|sender|a)(_.*)?$", numeric=False)
    dst = _find(cols, r"^(target|dst|to|child|callee|receiver|b|dependency|depends_on)(_.*)?$", numeric=False, exclude={src})
    return {"src": src, "dst": dst, "query": text[:500]} if src and dst else None


def _budget(text: str) -> float | None:
    m = re.search(r"budget (?:of )?[$€£]?\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand)?", text, re.I) \
        or re.search(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand)?\s*budget", text, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return v * (1e3 if unit in ("k", "thousand") else 1e6 if unit in ("m", "million") else 1)


def _fit_optimize(ds, text):
    cols = _cols(ds)
    budget = _budget(text)
    option = _find(cols, r"(option|channel|project|product|item|name|site|campaign)$", numeric=False)
    cost = _find(cols, r"(cost|price|cost_per_unit|unit_cost)$", numeric=True)
    value = _find(cols, r"(value|return|return_per_unit|profit|margin|revenue_per_unit|benefit)$", numeric=True, exclude={cost})
    mx = _find(cols, r"(max_units|max|capacity|limit|upper)$", numeric=True, exclude={cost, value})
    mn = _find(cols, r"(min_units|min|floor|lower)$", numeric=True, exclude={cost, value, mx})
    if not (budget and option and cost and value):
        return None
    return {"option": option, "cost": cost, "value": value, "max_units": mx, "min_units": mn, "budget": budget}


def _fit_text(ds, text):
    cols = _cols(ds)
    col = _find(cols, r"(comments?|text|feedback|description|body|message|notes?|review|transcript)$", numeric=False)
    group = _find(cols, r"(channel|category|product|segment|team|source)$", numeric=False, exclude={col})
    return {"text": col, "group": group} if col else None


_TARGET = r"(churn\w*|fraud\w*|default\w*|converted|conversion|label|target|outcome|failed|failure|is_\w+|has_\w+|cancel\w*)$"


def _features(cols, exclude: set[str]) -> list[str]:
    return [c["name"] for c in cols if _numeric(c) and c["name"] not in exclude and not _ID.search(c["name"])
            and not _TIME.search(c["name"]) and not re.search(_TARGET, c["name"], re.I)][:6]


def _fit_segment(ds, text):
    cols = _cols(ds)
    entity = _find(cols, _ID.pattern) or _find(cols, r"(name|customer|account|user)$", numeric=False)
    feats = _features(cols, {entity} if entity else set())
    return {"features": feats, "entity": entity} if len(feats) >= 2 else None


def _fit_classify(ds, text):
    cols = _cols(ds)
    named = [c["name"] for c in cols if re.search(rf"\b{re.escape(c['name'].replace('_', ' '))}\b", text.lower().replace("_", " "))]
    by = {c["name"]: c for c in cols}
    target = (next((n for n in named if re.search(_TARGET, n, re.I)), None)
              or next((n for n in named if not _numeric(by[n]) and not _ID.search(n) and not _TIME.search(n)), None)
              or _find(cols, _TARGET))
    if not target:
        return None
    ts = _time_col(cols)
    feats = _features(cols, {target})
    return {"target": target, "features": feats, "order_by": ts} if feats else None


def _fit_geo_pair(points, targets, text):
    pc, tc = _cols(points), _cols(targets)
    lat, lon = _find(pc, r"^(lat|latitude)$|_lat$"), _find(pc, r"^(lon|lng|long|longitude)$|_(lon|lng)$")
    tlat, tlon = _find(tc, r"^(lat|latitude)$|_lat$"), _find(tc, r"^(lon|lng|long|longitude)$|_(lon|lng)$")
    if not (lat and lon and tlat and tlon):
        return None
    km = re.search(r"(\d+(?:\.\d+)?)\s*(km|kilomet\w*|mi|miles?)\b", text, re.I)
    within = float(km.group(1)) * (1.609344 if km and km.group(2).lower().startswith("mi") else 1.0) if km else None
    return {"lat": lat, "lon": lon, "tlat": tlat, "tlon": tlon, "label": _find(pc, r"(name|label|store|site|id)$", numeric=False),
            "tlabel": _find(tc, r"(name|label|warehouse|site|id)$", numeric=False), "within_km": within}


def _fit_reconcile_pair(a, b, text):
    ca, cb = {c["name"]: c for c in _cols(a)}, {c["name"]: c for c in _cols(b)}
    shared = [n for n in ca if n in cb and not _numeric(ca[n])]
    key = next((n for n in shared if _ID.search(n) or re.search(r"(invoice|account|order|vendor|supplier|po|reference)", n, re.I)), None)
    key = key or (shared[0] if shared else None)
    amt = r"(amount|value|total|spend|cost|revenue|balance|net|gross)$"
    va = _find(list(ca.values()), amt, numeric=True)
    vb = _find(list(cb.values()), amt, numeric=True)
    return {"key": key, "value": va, "rvalue": vb} if key and va and vb else None


def _fit_compare(ds, text):
    cols = _cols(ds)
    group = _find(cols, r"(variant|group|arm|cohort|treatment|bucket|test_group)$", numeric=False)
    outcome = _find(cols, r"(converted|conversion|clicked|purchased|retained|outcome|revenue|value|score|signed_up|adverse_event|readmitted|responded)$",
                    exclude={group})
    return {"group": group, "outcome": outcome} if group and outcome else None


def _fit_eval(ds, text):
    cols = _cols(ds)
    passed = _find(cols, r"^(passed|correct|pass|success|is_correct)$")
    actual = _find(cols, r"^(actual|label|truth|y_true|true_label|observed)$")
    predicted = _find(cols, r"^(predicted|prediction|y_pred|predicted_label|forecast)$")
    segment = _find(cols, r"(segment|model|category|query_type|accent|modality|harm_category|class_group|region|type|case|"
                          r"asset|component|scenario|site)$",
                    numeric=False, exclude={actual, predicted, passed})
    if passed:
        return {"actual": passed, "passed": passed, "segment": segment}
    return {"actual": actual, "predicted": predicted, "segment": segment} if actual and predicted else None


METHODS: dict[str, dict[str, Any]] = {
    "optimize": {"title": "optimization", "task_classes": {"optimization", "capacity_planning", "recommendation"}, "arity": 1,
                 "fit": _fit_optimize, "words": r"\b(optimi[sz]\w*|allocat\w*|best (split|mix|allocation)|maximi[sz]e|budget)\b"},
    "text_themes": {"title": "text analytics", "task_classes": {"text_analytics", "research_synthesis"}, "arity": 1,
                    "fit": _fit_text, "words": r"\b(themes?|topics?|complain\w*|sentiment|comments|feedback|reviews|what .{0,30}"
                                               r"(say|saying))\b"},
    "evaluate": {"title": "model evaluation", "task_classes": {"model_evaluation", "statistical_testing"}, "arity": 1,
                 "fit": _fit_eval, "words": r"\b(evaluat\w*|accuracy|pass rate|precision|recall|how good|benchmark\w*|eval)\b"},
    "compare_groups": {"title": "two-group comparison", "task_classes": {"experiment_analysis", "statistical_testing", "causal_estimation"},
                       "arity": 1, "fit": _fit_compare,
                       "words": r"\b(a/?b|experiment\w*|variant\w*|test group|holdout|control|significan\w*|lift|uplift|beat|compare|"
                                r"versus|arms?|treatment)\b"},
    "process_map": {"title": "process mining", "task_classes": {"process_analysis"}, "arity": 1, "fit": _fit_process,
                    "words": r"\b(process|steps?|stuck|bottleneck|handoffs?|cycle time|rework|order to cash|order-to-cash|workflow|flow)\b"},
    "log_patterns": {"title": "event log analysis", "task_classes": {"security_investigation", "anomaly_detection", "root_cause"},
                     "arity": 1, "fit": _fit_log,
                     "words": r"\b(logs?|logins?|events?|telemetry|suspicious|burst|spike|unusual|alerts?|activity)\b"},
    "graph": {"title": "graph analysis", "task_classes": {"research_synthesis", "data_modeling", "segmentation", "risk_assessment"},
              "arity": 1, "fit": _fit_graph,
              "words": r"\b(connected|connections?|network|graph|links?|relationships?|dependenc\w*|related to)\b"},
    "reconcile": {"title": "two-source reconciliation", "task_classes": {"reconciliation"}, "arity": 2, "fit": _fit_reconcile_pair,
                  "words": r"\b(reconcil\w*|tie out|agree|match(es)?|differences?|breaks?|two sources|both sources)\b"},
    "distances": {"title": "distance analysis", "task_classes": {"geospatial_analysis"}, "arity": 2, "fit": _fit_geo_pair,
                  "words": r"\b(distance|km|miles?|far|nearest|closest|within|from (a|the|our) \w+)\b"},
    "classify": {"title": "classification", "task_classes": {"model_evaluation", "forecasting", "risk_assessment"}, "arity": 1,
                 "fit": _fit_classify, "words": r"\b(likely to|predict\w*|which .{0,30} will|classif\w*|propensity|at risk)\b"},
    "segment": {"title": "segmentation", "task_classes": {"segmentation"}, "arity": 1, "fit": _fit_segment,
                "words": r"\b(segment\w*|cluster\w*|groups?|personas?|tiers?|kinds of)\b"},
}


# words that appear in dataset names without saying what the data is about
_GENERIC_NAME = {"event", "data", "dataset", "table", "record", "list", "log", "link", "file", "export", "raw", "v1", "v2"}


def _named(text: str, datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Datasets the request names, best match first, then in the order the request mentions them. A dataset counts
    as named only when every distinctive word of its name appears ("login events" names login_events, "events" does
    not), so a method is never run on data the request did not point at."""
    q = tokens(text)
    qs = set(q)
    found = []
    for d in datasets:
        name_toks = set(tokens(d["name"].replace("_", " ")))
        required = (name_toks - _GENERIC_NAME) or name_toks
        overlap = qs & (name_toks | set(tokens(d.get("title") or "")))
        if overlap and required <= qs:
            found.append((overlap, d))
    shared = set.intersection(*(o for o, _ in found)) if len(found) > 1 else set()
    hits = []
    for overlap, d in found:
        own = (overlap - shared) or overlap  # "ledger erp" vs "ledger bank": the distinguishing word sets the order
        hits.append((len(overlap), min(q.index(t) for t in own), d))
    return [d for _, _, d in sorted(hits, key=lambda t: (-t[0], t[1]))]


def choose(text: str, task_classes: list[str], datasets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The method and inputs for a routed request, or None when no method fits the request and the data."""
    asked = set(task_classes)
    named = _named(text, datasets)
    order = sorted(METHODS, key=lambda m: (not (METHODS[m]["task_classes"] & asked), not re.search(METHODS[m]["words"], text, re.I)))
    for name in order:
        spec = METHODS[name]
        if not re.search(spec["words"], text, re.I):
            continue  # the request must ask for what the method does, in words, not only by task class
        if spec["arity"] == 1:
            fits = [(d, spec["fit"](d, text)) for d in named]
            fits = [(d, p) for d, p in fits if p]
            if fits:
                d, params = fits[0]
                return {"method": name, "title": spec["title"], "datasets": [d["name"]], "params": params,
                        "task_classes": sorted(spec["task_classes"])}
        else:
            for i, a in enumerate(named):
                for b in named[i + 1:]:
                    for x, y in ((a, b), (b, a)):
                        params = spec["fit"](x, y, text)
                        if params:
                            return {"method": name, "title": spec["title"], "datasets": [x["name"], y["name"]],
                                    "params": params, "task_classes": sorted(spec["task_classes"])}
    return None


BRIEF = {"title": "document brief", "task_classes": {"research_synthesis", "compliance_review", "text_analytics",
                                                     "document_extraction", "risk_assessment"},
         "words": r"\b(documents?|polic(y|ies)|brief me|summari[sz]e|say|says|said|requir\w*|promis\w*|recommend\w*|"
                  r"according to|contracts?|notes|findings|audit|assessment|review|register|readiness|survey|report|"
                  r"guidelines?|standard|evaluation|business case|requirements)\b"}
_STOPWORDS = {"what", "does", "do", "did", "our", "the", "say", "says", "about", "from", "with", "for", "and", "this", "that",
              "brief", "me", "summarize", "summarise", "document", "documents", "which", "how", "are", "is", "on", "of", "in", "to"}


def asks_about_documents(text: str) -> bool:
    """Wording that asks what a document says (checked before dataset methods, so "the order to cash report
    requirements" reads the requirements, not the order event log)."""
    return bool(re.search(r"\b(say|says|said|requir\w*|promis\w*|recommend\w*|according to|brief me|summari[sz]e the "
                          r"(findings|notes)|exceeds?)\b", text, re.I))


def choose_documents(text: str, task_classes: list[str], search, documents=None) -> dict[str, Any] | None:
    """A brief from the workspace's documents when the request asks about one and it clearly matches: a document whose
    title shares at least two distinctive words with the request, or a passage containing at least two of them."""
    if not re.search(BRIEF["words"], text, re.I) and not (BRIEF["task_classes"] & set(task_classes)):
        return None
    want = {t for t in tokens(text) if t not in _STOPWORDS and len(t) > 2}
    if documents is not None:
        try:
            docs = documents()
        except Exception:  # noqa: BLE001
            docs = []
        scored = sorted(((len(want & set(tokens(d["title"]))), d) for d in docs), key=lambda x: -x[0])
        if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            d = scored[0][1]
            return {"method": "brief", "title": BRIEF["title"], "datasets": [], "params": {"query": text, "document_id": d["id"]},
                    "task_classes": sorted(BRIEF["task_classes"])}
    try:
        hits = search(text)
    except Exception:  # noqa: BLE001 - no documents, or none the actor may read
        return None
    if not hits:
        return None
    have = set(tokens(str(hits[0]["text"]) + " " + str(hits[0]["title"])))
    if len(want & have) < 2:
        return None
    return {"method": "brief", "title": BRIEF["title"], "datasets": [], "params": {"query": text},
            "task_classes": sorted(BRIEF["task_classes"])}


@capability(REGISTRY, "run_method", agent="statistical_analyst", phase="execute", permission="data.query",
            description="Run a specialist method (segmentation, classification, process mining, distances, two-source "
                        "reconciliation, graph or event-log analysis) on datasets the actor may query.")
def run_method(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    from ..analyst.engine import _evidence  # noqa: PLC0415

    name, datasets, params = args["method"], args["datasets"], dict(args.get("params") or {})
    if name not in METHODS and name != "brief":
        raise ValueError(f"unknown method {name}")
    frames, evidence = [], []
    for ds in datasets:
        r = ctx.services.data.query(ctx.actor, ctx.workspace_id, f'SELECT * FROM "{ds}"', task_id=ctx.task_id, max_rows=MAX_ROWS)
        evidence.append(_evidence(r, f"{METHODS[name]['title']} input"))
        frames.append(r.frame())
    try:
        if name == "process_map":
            out = M.process_map(frames[0], params["case"], params["activity"], params["ts"])
        elif name == "log_patterns" and params.get("raw"):
            parsed = M.parse_logs(frames[0][params["raw"]])
            out = M.log_patterns(parsed, ts="ts", event="event", entity="entity")
            out["findings"].insert(0, f"Parsed {int((~parsed['_unparsed']).sum())} of {len(parsed)} lines "
                                      "(JSON, key=value, web access and syslog formats).")
        elif name == "log_patterns":
            out = M.log_patterns(frames[0], ts=params["ts"], event=params["event"], entity=params.get("entity"))
        elif name == "graph":
            out = M.graph(frames[0], params["src"], params["dst"], query=params.get("query"))
        elif name == "optimize":
            out = M.optimize_allocation(frames[0], option=params["option"], cost=params["cost"], value=params["value"],
                                        budget=params["budget"], max_units=params.get("max_units"),
                                        min_units=params.get("min_units"))
        elif name == "text_themes":
            out = M.text_themes(frames[0], params["text"], group=params.get("group"))
        elif name == "segment":
            out = M.segment(frames[0], params["features"], entity=params.get("entity"))
        elif name == "classify":
            out = M.classify(frames[0], params["target"], params["features"], order_by=params.get("order_by"))
        elif name == "evaluate":
            out = M.evaluate_predictions(frames[0], params["actual"], params.get("predicted"), segment=params.get("segment"),
                                         passed=params.get("passed"))
        elif name == "compare_groups":
            out = M.compare_groups(frames[0], params["group"], params["outcome"])
        elif name == "brief":
            if params.get("document_id"):  # the document the request names: its passages, best matching first
                passages = ctx.services.knowledge.document_passages(ctx.actor, ctx.workspace_id, params["document_id"])
                want = set(tokens(params["query"]))
                passages.sort(key=lambda p: -len(want & set(tokens(p["text"]))))
            else:
                passages = ctx.services.knowledge.search(ctx.actor, ctx.workspace_id, params["query"], limit=5)
            out = M.brief(passages, params["query"])
        elif name == "reconcile":
            out = M.reconcile(frames[0], frames[1], key=params["key"], value=params["value"], rvalue=params.get("rvalue"),
                              names=(datasets[0], datasets[1]))
        else:
            out = M.distances(frames[0], frames[1], lat=params["lat"], lon=params["lon"], tlat=params["tlat"], tlon=params["tlon"],
                              label=params.get("label"), tlabel=params.get("tlabel"), within_km=params.get("within_km"))
    except M.MethodError as exc:
        return {"method": name, "datasets": datasets, "params": params, "error": str(exc), "evidence": evidence}
    title = METHODS[name]["title"] if name in METHODS else BRIEF["title"]
    return {"method": name, "title": title, "datasets": datasets, "params": params, "output": out, "evidence": evidence}
