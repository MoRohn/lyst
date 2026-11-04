# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Living business knowledge: entities, relations (a graph in DuckDB tables), lineage, documents with
citations, and consistency against prior analyses.

* **Entities** are the things the organization talks about: business units, teams, systems, processes,
  datasets, metrics. **Relations** connect them (`owns`, `reads`, `feeds`, `derived_from`, `produced`),
  each with provenance. Lineage is a walk over relations: a metric's upstream datasets and columns, its
  downstream analyses and artifacts.
* Lyst writes relations itself as work happens: defining a metric records `metric reads dataset.column`,
  finishing an analysis records `analysis used metric` and `analysis read dataset@version`, building a
  deliverable records `artifact produced_from analysis`. People add the organizational layer.
* **Documents** (text, Markdown, CSV notes, PDF) are split into passages and searched with TF-IDF over
  the workspace's own vocabulary; an answer cites the passage, never a paraphrase. Document access
  follows workspace membership and the document's classification.
* **Consistency**: before answering, the same question on the same dataset version is looked up; a
  different earlier answer is reported, not hidden.

No graph database: a few thousand relations in DuckDB answer every lineage question in milliseconds.
"""

from __future__ import annotations

import io
import json
import math
import re
import uuid
from collections import Counter
from typing import Any

from .security import RANK, Guard, Principal
from .store import Stores

KINDS = ("business_unit", "team", "person", "system", "process", "dataset", "column", "metric", "analysis",
         "artifact", "document", "objective")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{2,}")
STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have", "not", "but",
        "our", "its", "per", "all", "any", "can", "will", "than", "then", "into", "over", "under", "about"}


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOP]


def split_passages(text: str, *, max_chars: int = 900) -> list[str]:
    parts: list[str] = []
    heading = ""
    for block in re.split(r"\n\s*\n", text):
        block = " ".join(block.split())
        if not block:
            continue
        if len(block) < 80 and not block.endswith((".", "!", "?", ":")):
            heading = block.lstrip("# ").strip()  # a heading: carried into the next passage
            continue
        if heading:
            block, heading = f"{heading}. {block}", ""
        while len(block) > max_chars:
            cut = block.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            parts.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            parts.append(block)
    return parts


def extract_text(content: bytes, filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader  # noqa: PLC0415

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return content.decode("utf-8", errors="replace")


class KnowledgeService:
    def __init__(self, stores: Stores, guard: Guard) -> None:
        self.stores = stores
        self.guard = guard

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- entities and relations -------------------------------------------------------------

    def upsert_entity(self, actor: Principal, ws: str, kind: str, name: str, *, description: str = "",
                      owner: str | None = None, attributes: dict | None = None, source: str = "user") -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown entity kind {kind}")
        if actor.kind != "system":
            self.guard.require(actor, ws, "glossary.write" if source == "user" else "workspace.read", f"{kind}:{name}")
        self._db(ws).execute(
            "INSERT OR REPLACE INTO entities (kind, name, description, owner, attributes, source, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [kind, name, description, owner, json.dumps(attributes or {}), source, actor.user_id])

    def relate(self, actor: Principal, ws: str, from_kind: str, from_name: str, rel: str, to_kind: str, to_name: str,
               *, evidence: dict | None = None, source: str = "system") -> None:
        if actor.kind != "system" and source == "user":
            self.guard.require(actor, ws, "glossary.write", rel)
        for kind, name in ((from_kind, from_name), (to_kind, to_name)):
            self._db(ws).execute(
                "INSERT INTO entities (kind, name, description, owner, attributes, source, updated_by) "
                "SELECT ?, ?, '', NULL, '{}', ?, ? WHERE NOT EXISTS (SELECT 1 FROM entities WHERE kind = ? AND name = ?)",
                [kind, name, source, actor.user_id, kind, name])
        self._db(ws).execute(
            "INSERT OR REPLACE INTO relations (from_kind, from_name, rel, to_kind, to_name, evidence, source, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [from_kind, from_name, rel, to_kind, to_name, json.dumps(evidence or {}, default=str), source, actor.user_id])

    def entities(self, actor: Principal, ws: str, kind: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql = "SELECT kind, name, description, owner, attributes, source, updated_at FROM entities"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        rows = self._db(ws).query(sql + " ORDER BY kind, name", params)
        for r in rows:
            r["attributes"] = json.loads(r["attributes"])
        return rows

    def relations(self, actor: Principal, ws: str, kind: str | None = None, name: str | None = None) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        sql = "SELECT from_kind, from_name, rel, to_kind, to_name, evidence, source, created_at FROM relations"
        params: list[Any] = []
        if kind and name:
            sql += " WHERE (from_kind = ? AND from_name = ?) OR (to_kind = ? AND to_name = ?)"
            params = [kind, name, kind, name]
        rows = self._db(ws).query(sql + " ORDER BY created_at", params)
        for r in rows:
            r["evidence"] = json.loads(r["evidence"])
        return rows

    def lineage(self, actor: Principal, ws: str, kind: str, name: str, *, depth: int = 4) -> dict[str, Any]:
        """Everything upstream (what this depends on) and downstream (what depends on it)."""
        self.guard.require(actor, ws, "workspace.read")
        db = self._db(ws)

        def walk(direction: str) -> list[dict[str, Any]]:
            seen, frontier, out = {(kind, name)}, [(kind, name, 0)], []
            while frontier:
                k, n, d = frontier.pop()
                if d >= depth:
                    continue
                if direction == "up":
                    rows = db.query("SELECT to_kind AS k, to_name AS n, rel FROM relations WHERE from_kind = ? AND from_name = ?", [k, n])
                else:
                    rows = db.query("SELECT from_kind AS k, from_name AS n, rel FROM relations WHERE to_kind = ? AND to_name = ?", [k, n])
                for r in rows:
                    key = (r["k"], r["n"])
                    out.append({"kind": r["k"], "name": r["n"], "rel": r["rel"], "via": n, "depth": d + 1})
                    if key not in seen:
                        seen.add(key)
                        frontier.append((r["k"], r["n"], d + 1))
            return out

        return {"kind": kind, "name": name, "upstream": walk("up"), "downstream": walk("down")}

    # -- automatic lineage hooks ------------------------------------------------------------

    def record_metric(self, actor: Principal, ws: str, spec) -> None:
        self.upsert_entity(actor, ws, "metric", spec.name, description=spec.description or spec.title, owner=actor.user_id,
                           attributes={"title": spec.title, "kind": spec.kind, "format": spec.format}, source="system")
        self.relate(actor, ws, "metric", spec.name, "reads", "dataset", spec.dataset, source="system")
        for col in sorted(spec.columns()):
            self.relate(actor, ws, "metric", spec.name, "reads", "column", f"{spec.dataset}.{col}", source="system")
            self.relate(actor, ws, "column", f"{spec.dataset}.{col}", "belongs_to", "dataset", spec.dataset, source="system")

    def record_analysis(self, actor: Principal, ws: str, analysis_id: str, question: str, metric: str | None,
                        sources: list[dict[str, Any]]) -> None:
        self.upsert_entity(actor, ws, "analysis", analysis_id, description=question[:200], owner=actor.user_id, source="system")
        if metric:
            self.relate(actor, ws, "analysis", analysis_id, "used", "metric", metric, source="system")
        for s in sources:
            self.relate(actor, ws, "analysis", analysis_id, "read", "dataset", s["dataset"],
                        evidence={"version": s.get("version"), "sha256": s.get("sha256")}, source="system")

    def record_artifact(self, actor: Principal, ws: str, artifact_id: str, task_id: str, kind: str) -> None:
        aid = self._db(ws).scalar("SELECT id FROM analyses WHERE task_id = ?", [task_id])
        if aid:
            self.relate(actor, ws, "artifact", artifact_id, "produced_from", "analysis", aid, evidence={"kind": kind}, source="system")

    # -- documents --------------------------------------------------------------------------

    def add_document(self, actor: Principal, ws: str, content: bytes, *, filename: str, title: str | None = None,
                     classification: str = "internal", min_role: str = "viewer") -> dict[str, Any]:
        self.guard.require(actor, ws, "data.ingest", filename)
        if min_role not in RANK:
            raise ValueError("unknown role")
        text = extract_text(content, filename)
        passages = split_passages(text)
        if not passages:
            raise ValueError("no readable text in the document")
        did = "d_" + uuid.uuid4().hex[:10]
        db = self._db(ws)

        def tx(c):
            c.execute("INSERT INTO documents (id, title, filename, classification, min_role, passages, chars, added_by) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      [did, title or filename, filename, classification, min_role, len(passages), len(text), actor.user_id])
            for i, p in enumerate(passages):
                c.execute("INSERT INTO passages (document_id, seq, text, terms) VALUES (?, ?, ?, ?)",
                          [did, i, p, json.dumps(dict(Counter(tokens(p))))])

        db.write(tx)
        self.upsert_entity(actor, ws, "document", did, description=title or filename, owner=actor.user_id,
                           attributes={"filename": filename, "classification": classification}, source="system")
        self.guard.audit(actor, ws, "document.add", did, "ok", {"passages": len(passages)})
        return {"id": did, "title": title or filename, "passages": len(passages)}

    def documents(self, actor: Principal, ws: str) -> list[dict[str, Any]]:
        role = self.guard.require(actor, ws, "workspace.read")
        rows = self._db(ws).query("SELECT id, title, filename, classification, min_role, passages, added_by, added_at FROM documents ORDER BY added_at DESC")
        return [r for r in rows if RANK[role] >= RANK[r["min_role"]]]

    def search(self, actor: Principal, ws: str, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """TF-IDF over passages the caller may read; returns passages with their document and a score."""
        role = self.guard.require(actor, ws, "workspace.read")
        q = Counter(tokens(query))
        if not q:
            return []
        db = self._db(ws)
        rows = db.query("SELECT p.document_id, p.seq, p.text, p.terms, d.title, d.min_role FROM passages p "
                        "JOIN documents d ON d.id = p.document_id")
        rows = [r for r in rows if RANK[role] >= RANK[r["min_role"]]]
        if not rows:
            return []
        n = len(rows)
        df: Counter = Counter()
        parsed = []
        for r in rows:
            terms = json.loads(r["terms"])
            parsed.append(terms)
            df.update(terms.keys())
        avg_len = sum(sum(t.values()) for t in parsed) / n
        k1, b = 1.2, 0.75  # BM25 saturation and length normalisation
        scored = []
        for r, terms in zip(rows, parsed, strict=True):
            length = sum(terms.values()) or 1
            score = 0.0
            for t, qf in q.items():
                if t in terms:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    tf = terms[t]
                    score += qf * idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / avg_len))
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"document_id": r["document_id"], "title": r["title"], "passage": r["seq"], "text": r["text"],
                 "score": round(s, 4)} for s, r in scored[:limit]]

    # -- consistency with earlier work ------------------------------------------------------

    def prior_answers(self, actor: Principal, ws: str, question: str, *, limit: int = 3) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "workspace.read")
        key = " ".join(tokens(question))
        rows = self._db(ws).query(
            "SELECT id, task_id, question, answer, evidence, created_at FROM analyses ORDER BY created_at DESC LIMIT 200")
        out = []
        for r in rows:
            if " ".join(tokens(r["question"])) == key:
                ans, ev = json.loads(r["answer"]), json.loads(r["evidence"])
                out.append({"analysis_id": r["id"], "task_id": r["task_id"], "answer": ans.get("answer"),
                            "status": ans.get("status"), "sources": ev.get("sources", []), "at": str(r["created_at"])})
            if len(out) >= limit:
                break
        return out
