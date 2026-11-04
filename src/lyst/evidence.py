# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The evidence graph behind an answer, and explanations answered from it.

Nothing here is generated: every node and edge is read from records the engine already keeps while it works
(the task and its result, each step's stored output, the query log, dataset versions, metric versions, task events,
approvals and model calls). The graph connects

    claim (the answer, each finding) -> figure (a number stated in it) -> step (the capability that computed it)
      -> query (the SQL that ran) -> dataset version (rows, checksum, load time) -> source (file or connection)
    figure -> metric definition version -> dataset
    validation check / assumption / cited passage / model wording -> answer

and is stored as plain records in DuckDB; no graph database is needed at this size.

A figure is *traced* when a step's stored output holds the same number within display rounding (the same tolerance
the engine uses to reject a model's unbacked numbers). A figure that matches no step is reported as untraced, never
attributed by guess.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .analyst.engine import _numbers
from .security import Guard, Principal
from .store import Stores

QUESTIONS = ("how", "sources", "method", "assumptions", "change", "reproduce", "actions")
_SKIP_STEPS = ("understand", "plan", "deliver", "learn", "contextualize")


def _numbers_with_paths(obj: Any, path: str = "") -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, int | float):
        if math.isfinite(float(obj)):
            out.append((path, float(obj)))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("evidence", "query_id", "rows", "ms"):
                continue  # bookkeeping, not results
            out += _numbers_with_paths(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            out += _numbers_with_paths(v, f"{path}[{i}]")
    return out


def _backs(stated: float, computed: float) -> bool:
    """Does a computed value back a stated figure at the precision it was written (12.3% for 0.12345)?"""
    for c in (computed, computed * 100):
        if math.isclose(abs(stated), abs(c), rel_tol=0.006, abs_tol=0.0006):
            return True
        text = repr(abs(stated))
        places = len(text.split(".")[1]) if "." in text and "e" not in text else 0
        if round(abs(c), places) == abs(stated):
            return True
    return False


_TRIVIAL = re.compile(r"^(?:19|20)\d\d$")


class EvidenceService:
    def __init__(self, stores: Stores, guard: Guard, runtime, artifacts=None) -> None:
        self.stores = stores
        self.guard = guard
        self.runtime = runtime
        self.artifacts = artifacts

    def for_artifact(self, actor: Principal, artifact_id: str) -> dict[str, Any]:
        """The graph behind a generated report, workbook or deck: the artifact presents the answer of the task
        that produced it, so a figure in the report traces the same way."""
        meta, _ = self.artifacts.get(actor, artifact_id)  # enforces access to the artifact
        g = self.graph(actor, meta["task_id"])
        g["nodes"].append({"id": f"artifact:{artifact_id}", "kind": "artifact", "label": f"{meta['kind']} report",
                           "sha256": meta.get("sha256"), "created_at": str(meta.get("created_at"))})
        g["edges"].append({"from": f"artifact:{artifact_id}", "to": "answer", "rel": "presents"})
        return g

    # -- graph ----------------------------------------------------------------------------------

    def graph(self, actor: Principal, task_id: str) -> dict[str, Any]:
        task = self.runtime.get(actor, task_id)  # enforces workspace.read
        ws = task["workspace_id"]
        db = self.stores.workspace(ws)
        result = task["result"] or {}
        sup = result.get("supporting") or {}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []

        def node(nid: str, kind: str, label: str, **attrs) -> str:
            nodes.setdefault(nid, {"id": nid, "kind": kind, "label": label, **attrs})
            return nid

        def edge(a: str, b: str, rel: str) -> None:
            e = {"from": a, "to": b, "rel": rel}
            if e not in edges:
                edges.append(e)

        answer = node("answer", "answer", result.get("answer") or task["title"], status=result.get("status"),
                      question=result.get("question") or (task["request"] or {}).get("question"),
                      confidence=(result.get("confidence") or {}).get("level"), narrative_by=sup.get("narrative_by"))

        # steps and the queries they ran
        steps = self.stores.control.query("SELECT seq, capability, agent, status, output, error FROM task_steps WHERE task_id = ? ORDER BY seq",
                                          [task_id])
        step_numbers: dict[int, list[tuple[str, float]]] = {}
        query_ids: set[int] = set()
        for s in steps:
            out = json.loads(s["output"]) if s["output"] else {}
            sid = node(f"step:{s['seq']}", "step", s["capability"], agent=s["agent"], status=s["status"], error=s["error"],
                       description=self._describe(s["capability"]))
            if s["capability"] not in _SKIP_STEPS:
                step_numbers[s["seq"]] = _numbers_with_paths(out)
            for ev in (out.get("evidence") or []) if isinstance(out, dict) else []:
                if not isinstance(ev, dict) or ev.get("query_id") is None:  # some steps list findings as text
                    continue
                qid = int(ev["query_id"])
                query_ids.add(qid)
                node(f"query:{qid}", "query", ev.get("purpose") or "query", sql=ev.get("sql"), rows=ev.get("rows"), ms=ev.get("ms"),
                     datasets=ev.get("datasets") or [])
                edge(sid, f"query:{qid}", "ran")

        # dataset versions and where they came from, as of the task's run
        versions = {(s["dataset"], s["version"]) for s in sup.get("sources", [])}
        names_read = {d for n in nodes.values() if n["kind"] == "query" for d in n["datasets"]}
        for name in names_read:
            if not any(v[0] == name for v in versions):
                v = db.scalar("SELECT max(version) FROM dataset_versions WHERE dataset = ? AND created_at <= ?", [name, task["created_at"]])
                if v:
                    versions.add((name, v))
        for name, ver in sorted(versions):
            rows = db.query("SELECT source, source_sha256, row_count, created_by, created_at FROM dataset_versions WHERE dataset = ? AND version = ?",
                            [name, ver])
            if not rows:
                continue
            r = rows[0]
            current = db.scalar("SELECT current_version FROM datasets WHERE name = ?", [name])
            did = node(f"dataset:{name}@{ver}", "dataset_version", f"{name} v{ver}", dataset=name, version=ver, rows=r["row_count"],
                       sha256=r["source_sha256"], loaded_at=str(r["created_at"]), loaded_by=r["created_by"], current_version=current,
                       classification=db.scalar("SELECT classification FROM datasets WHERE name = ?", [name]))
            src = node(f"source:{r['source_sha256'][:16]}", "source", r["source"], sha256=r["source_sha256"])
            edge(did, src, "loaded_from")
            for q in [n for n in nodes.values() if n["kind"] == "query" and name in n["datasets"]]:
                edge(q["id"], did, "read")

        # the governed definition in effect when the task ran
        metric = (sup.get("metric") or {}).get("spec")
        mid = None
        if metric:
            mver = db.scalar("SELECT max(version) FROM metric_versions WHERE name = ? AND created_at <= ?", [metric["name"], task["created_at"]])
            mid = node(f"metric:{metric['name']}@{mver}", "metric_version", f"{metric.get('title') or metric['name']} v{mver}",
                       metric=metric["name"], version=mver, definition=self._definition(metric))
            for n in [n for n in nodes.values() if n["kind"] == "dataset_version" and n["dataset"] == metric["dataset"]]:
                edge(mid, n["id"], "over")

        # claims and the figures in them
        claims = [("answer", result.get("answer") or "")] + [(f"finding:{i}", f) for i, f in enumerate(result.get("evidence") or [])]
        figures, untraced = [], []
        for cid, text in claims:
            if cid != "answer":
                node(cid, "claim", text)
                edge(cid, answer, "supports")
            for j, (tok, val) in enumerate(self._figures(text)):
                fid = f"figure:{cid}:{j}"
                hits = [(seq, p) for seq, nums in step_numbers.items() for p, v in nums if _backs(val, v)]
                node(fid, "figure", tok, value=val, traced=bool(hits), computed_at=[f"step:{seq}:{p}" for seq, p in hits[:5]])
                edge(cid, fid, "states")
                for seq in sorted({h[0] for h in hits}):
                    edge(fid, f"step:{seq}", "computed_by")
                if mid and hits:
                    edge(fid, mid, "defined_by")
                (figures if hits else untraced).append(fid)

        # what conditions or checks the answer
        for i, c in enumerate((result.get("confidence") or {}).get("checks") or []):
            cid = node(f"check:{i}", "check", c.get("name", "check"), passed=c.get("passed"), severity=c.get("severity"), detail=c.get("detail"))
            edge(cid, answer, "verifies" if c.get("passed") else "warns")
        for i, a in enumerate(sup.get("assumptions") or []):
            edge(node(f"assumption:{i}", "assumption", a), answer, "conditions")
        for c in sup.get("citations") or []:
            edge(node(f"passage:{c['document_id']}#{c['passage']}", "passage", c["title"], quote=c.get("quote")), answer, "cited_by")
        for a in self.stores.control.query("SELECT id, action, summary, status, decided_by, decided_at FROM approvals WHERE task_id = ?", [task_id]):
            edge(node(f"approval:{a['id']}", "decision", a["summary"], action=a["action"], status=a["status"], decided_by=a["decided_by"],
                      decided_at=str(a["decided_at"]) if a["decided_at"] else None), answer, "authorized")
        calls = self.stores.control.scalar("SELECT count(*) FROM llm_calls WHERE task_id = ? AND purpose = 'deliver' AND outcome = 'ok'", [task_id])
        if calls:
            edge(node("model:deliver", "model", "model wording", narrative_by=sup.get("narrative_by")), answer, "worded")

        return {"task_id": task_id, "workspace_id": ws, "question": nodes[answer]["question"], "status": result.get("status"),
                "nodes": list(nodes.values()), "edges": edges,
                "figures": {"traced": len(figures), "untraced": len(untraced), "untraced_ids": untraced},
                "currency": self._currency(nodes)}

    @staticmethod
    def _figures(text: str) -> list[tuple[str, float]]:
        out = []
        for m in re.finditer(r"[-+]?\$?\d[\d,]*\.?\d*\s*(?:%|pts|points|percentage points|[kKmMbB](?![a-z])|thousand|million|billion)?", text or ""):
            tok = m.group(0).strip().rstrip(".,")
            if _TRIVIAL.match(tok.replace(",", "")):
                continue
            vals = _numbers(tok)
            if vals and vals[0] not in (0, 1, 2, 3, 4):
                out.append((tok, vals[0]))
        return out

    def _describe(self, capability: str) -> str | None:
        reg = getattr(getattr(self.runtime, "services", None), "registry", None)
        try:
            return reg.get(capability).description if reg else None
        except Exception:  # noqa: BLE001 - a capability retired since the run
            return None

    @staticmethod
    def _definition(spec: dict[str, Any]) -> str:
        from .semantic import MetricSpec, value_expr  # noqa: PLC0415

        try:
            return value_expr(MetricSpec(**spec))
        except Exception:  # noqa: BLE001
            return json.dumps(spec.get("measure") or {})

    @staticmethod
    def _currency(nodes: dict[str, dict[str, Any]] | list) -> dict[str, Any]:
        vals = nodes.values() if isinstance(nodes, dict) else nodes
        changed = [{"dataset": n["dataset"], "used": n["version"], "current": n["current_version"]}
                   for n in vals if n["kind"] == "dataset_version" and n["current_version"] != n["version"]]
        return {"current": not changed, "changed": changed}

    # -- explanations ---------------------------------------------------------------------------

    def explain(self, actor: Principal, task_id: str, question: str = "how", *, figure: str | None = None) -> dict[str, Any]:
        """Answer one of the standard trust questions from the graph. ``short`` is one or two sentences;
        ``detail`` holds the records behind it."""
        if question not in QUESTIONS:
            raise ValueError(f"question is one of {', '.join(QUESTIONS)}")
        g = self.graph(actor, task_id)
        by = {n["id"]: n for n in g["nodes"]}
        out = {"task_id": task_id, "question": question}

        def targets(src: str, rel: str) -> list[dict[str, Any]]:
            return [by[e["to"]] for e in g["edges"] if e["from"] == src and e["rel"] == rel and e["to"] in by]

        if question == "how":
            figs = [n for n in g["nodes"] if n["kind"] == "figure"]
            if figure:
                figs = [n for n in figs if n["label"] == figure or n["id"] == figure] or \
                       [n for n in figs if (v := _numbers(figure)) and _backs(v[0], n["value"])]
            paths = []
            for f in figs:
                steps = targets(f["id"], "computed_by")
                queries = [q for s in steps for q in targets(s["id"], "ran")]
                data = [d for q in queries for d in targets(q["id"], "read")]
                metric = targets(f["id"], "defined_by")
                paths.append({"figure": f["label"], "traced": f["traced"],
                              "steps": [{"capability": s["label"], "description": s.get("description")} for s in steps],
                              "definition": [{"metric": m["metric"], "version": m["version"], "expression": m["definition"]} for m in metric],
                              "queries": [{"id": q["id"], "sql": q["sql"], "rows": q["rows"]} for q in queries],
                              "data": sorted({d["label"] for d in data})})
            first = next((p for p in paths if p["traced"]), None)
            if first:
                bits = [f"{first['figure']} was computed by {', '.join(dict.fromkeys(s['capability'] for s in first['steps']))}"]
                if first["definition"]:
                    d = first["definition"][0]
                    bits.append(f"using the approved {d['metric']} definition v{d['version']} ({d['expression']})")
                if first["data"]:
                    bits.append(f"over {', '.join(first['data'])}")
                out["short"] = " ".join(bits) + "."
            else:
                out["short"] = "No figure in this answer could be traced to a computed value." if paths else "This answer states no figures."
            out["detail"] = paths
        elif question == "sources":
            data = [n for n in g["nodes"] if n["kind"] == "dataset_version"]
            passages = [n for n in g["nodes"] if n["kind"] == "passage"]
            out["short"] = ("Computed from " + "; ".join(f"{d['label']} ({d['rows']:,} rows, loaded {d['loaded_at'][:10]})" for d in data)
                            + (f", citing {len(passages)} document passage{'s' if len(passages) != 1 else ''}" if passages else "") + ".") \
                if data else "No dataset was read for this answer."
            out["detail"] = {"datasets": data, "passages": passages,
                             "sources": [n for n in g["nodes"] if n["kind"] == "source"]}
        elif question == "method":
            steps = [n for n in g["nodes"] if n["kind"] == "step"]
            out["short"] = "Ran " + ", then ".join(s["label"].replace("_", " ") for s in steps) + "." if steps else "No steps ran."
            out["detail"] = [{"capability": s["label"], "agent": s["agent"], "why": s.get("description"), "status": s["status"]} for s in steps]
        elif question == "assumptions":
            a = [n["label"] for n in g["nodes"] if n["kind"] == "assumption"]
            out["short"] = a[0] if len(a) == 1 else (f"{len(a)} assumptions; the first: {a[0]}" if a else "No assumptions were recorded.")
            out["detail"] = a
        elif question == "change":
            warns = [n for n in g["nodes"] if n["kind"] == "check" and not n.get("passed")]
            cur = g["currency"]
            reasons = [f"{c['dataset']} has a newer version (v{c['current']}, this used v{c['used']})" for c in cur["changed"]]
            reasons += [f"{w['label']}: {w['detail']}" for w in warns]
            reasons += [n["label"] for n in g["nodes"] if n["kind"] == "assumption"][:3]
            out["short"] = ("This could change if: " + "; ".join(r.rstrip(". ") for r in reasons[:3]) + ".") if reasons else \
                "Nothing recorded suggests this would change: the data is current and every check passed."
            out["detail"] = {"currency": cur, "warnings": warns, "assumptions": [n["label"] for n in g["nodes"] if n["kind"] == "assumption"]}
        elif question == "reproduce":
            return {**out, **self.reproduce(actor, task_id, graph=g)}
        else:  # actions: why Lyst did what it did, from the task's own events
            task = self.runtime.get(actor, task_id)
            events = [{"when": str(e["occurred_at"]), "kind": e["kind"], "why": e["message"]} for e in task["events"]]
            out["short"] = (task.get("error") and f"It failed: {task['error']}") or \
                (events[-1]["why"] if events else f"The task is {task['status']}.")
            out["detail"] = events
        return out

    def reproduce(self, actor: Principal, task_id: str, *, graph: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run every recorded query again, as the caller and under the caller's policies, and compare row counts.
        A query over a dataset that has a newer version is reported as not comparable rather than re-run."""
        g = graph or self.graph(actor, task_id)
        ws = g["workspace_id"]
        data = self.runtime.services.data
        changed = {c["dataset"] for c in g["currency"]["changed"]}
        results = []
        for q in [n for n in g["nodes"] if n["kind"] == "query"]:
            if set(q["datasets"]) & changed:
                results.append({"query": q["id"], "outcome": "not_comparable", "why": "the data has a newer version"})
                continue
            try:
                r = data.query(actor, ws, q["sql"])
            except Exception as exc:  # noqa: BLE001 - a policy or definition change is a result to report
                results.append({"query": q["id"], "outcome": "could_not_run", "why": str(exc)[:200]})
                continue
            results.append({"query": q["id"], "outcome": "same" if len(r.rows) == q["rows"] else "different",
                            "rows_then": q["rows"], "rows_now": len(r.rows)})
        same = sum(r["outcome"] == "same" for r in results)
        short = (f"Re-ran {len(results)} quer{'y' if len(results) == 1 else 'ies'}: {same} returned the same rows"
                 + ("" if same == len(results) else f", {len(results) - same} did not") + ".") if results else "There were no queries to re-run."
        return {"short": short, "reproducible": bool(results) and same == len(results), "detail": results}
