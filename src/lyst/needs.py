# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What Lyst asks when it needs someone: each answer that needs input (or did not pass verification) as one question
with choices, and the choice a person picks carried out by Lyst.

The choices are worked out from the workspace, never guessed by a model: the approved metrics a word matches, the
datasets there are, the checks that failed. Each one says what it does before it is picked:

* ``ask`` runs the request again, reworded to the choice ("What was operating margin last quarter by product
  line?"); the first answer is kept and the new one records what it resolves.
* ``map`` (admins) records a glossary mapping ("gross margin" means operating margin), then asks again. It changes
  how everyone's requests are read, so the person confirms it first, and it is audited like any glossary write.
* ``request`` (editors) asks an admin for that change through a review (``glossary.map``) or asks an admin to
  look (``look``); nothing changes until an admin who is not the requester approves it.
* ``open`` goes where the fix is made (the data quality review table, Work › Approvals, a metric's definition);
  the item stays open until someone picks a choice that resolves it.
* ``dismiss`` records that the person no longer needs it ("not needed", or "keep it, not verified").

Anything else is written in words: it runs as a clarification of the same request. A resolution never edits the
answer; it records the choice (``task_resolutions``) so the item stops counting as waiting on anyone.
"""

from __future__ import annotations

import re
from typing import Any

from .security import RANK, AccessDenied, Guard, Principal
from .store import Stores

KINDS = ("ask", "map", "request", "open", "dismiss", "other")
# words that never sharpen a metric term: "what was our margin" asks about margin, not "our margin"
_STOP = {
    "a", "an", "the", "our", "my", "their", "its", "this", "that", "these", "those", "what", "which", "was", "is", "are",
    "were", "be", "of", "for", "in", "on", "by", "to", "and", "or", "with", "last", "next", "previous", "current", "show",
    "me", "us", "how", "did", "does", "do", "why", "total", "average", "monthly", "quarterly", "yearly", "annual", "q1",
    "q2", "q3", "q4", "year", "quarter", "month", "week", "compare", "trend", "forecast", "explain", "about", "give",
}


def _phrase(question: str, term: str) -> str:
    """The words the person used for an ambiguous term, with the one word in front that sharpens it ("gross margin"
    for the term "margin"), so a choice can replace exactly what they wrote."""
    m = re.search(rf"\b([a-z][a-z0-9-]*)\s+{re.escape(term)}(?:e?s)?\b", question.lower())
    if m and m.group(1) not in _STOP:
        return f"{m.group(1)} {term}"
    return term


def _reword(question: str, phrase: str, title: str) -> str:
    """The request with the person's words for the metric replaced by the metric's own name."""
    out, n = re.subn(rf"\b{re.escape(phrase)}(?:e?s)?\b", _low(title), question, count=1, flags=re.I)
    return out if n else f"{question.rstrip(' ?.')} (as {_low(title)})?"


def _low(title: str) -> str:
    """A metric's name inside a sentence: lower case, but acronyms stay as written ("COGS ratio")."""
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in title.split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}


class NeedsService:
    def __init__(self, stores: Stores, guard: Guard, runtime, semantic, data, collab=None) -> None:
        self.stores = stores
        self.guard = guard
        self.runtime = runtime
        self.semantic = semantic
        self.data = data
        self.collab = collab

    # -- reading ------------------------------------------------------------------------------------------------

    def view(self, actor: Principal, task_id: str) -> dict[str, Any]:
        """The question Lyst asks about this answer and the choices it offers this person, or ``needs: False`` when
        the answer does not need anyone. A resolved answer carries how it was resolved instead of choices."""
        t = self.runtime.get(actor, task_id)
        ws = t["workspace_id"]
        r = t.get("result") or {}
        question = ((t.get("request") or {}).get("question") or t["title"]).strip()
        base = {"task_id": task_id, "question": question, "status": r.get("status"), "why": r.get("answer") or "",
                "resolution": t.get("resolution"), "can_resolve": self._may_resolve(actor, t)}
        if t["status"] != "done" or r.get("status") not in ("needs_input", "unverified"):
            return {**base, "needs": False, "issue": "", "choices": []}
        role = self.guard.role(actor, ws) or "viewer"
        issue, choices = (self._input(actor, ws, question, r, role) if r["status"] == "needs_input"
                          else self._unverified(question, r))
        if not choices or choices[-1]["kind"] != "dismiss":
            choices.append({"id": "dismiss", "kind": "dismiss", "label": "Not needed any more",
                            "detail": "Clears it from what needs you. The answer stays as it is."})
        for c in choices:
            c.setdefault("detail", "")
            c.setdefault("confirm", None)
            c.setdefault("recommended", False)
        return {**base, "needs": True, "issue": issue, "choices": choices,
                "other": {"placeholder": "Something else: tell Lyst what you mean"}}

    def _may_resolve(self, actor: Principal, t: dict[str, Any]) -> bool:
        if actor.user_id == t["created_by"]:
            return True
        return RANK.get(self.guard.role(actor, t["workspace_id"]) or "", 0) >= RANK["editor"]

    def _metrics(self, actor: Principal, ws: str) -> dict[str, Any]:
        return {m.name: m for m in self.semantic.metrics(actor, ws)}

    def _admins(self, ws: str, but: str) -> list[str]:
        return [r["user_id"] for r in self.stores.control.query(
            "SELECT user_id FROM memberships WHERE workspace_id = ? AND role IN ('owner', 'admin') AND user_id != ? "
            "ORDER BY role DESC, user_id", [ws, but])]

    def _input(self, actor: Principal, ws: str, question: str, r: dict[str, Any], role: str) -> tuple[str, list[dict]]:
        answer = r.get("answer") or ""
        rank = RANK.get(role, 0)
        metrics = self._metrics(actor, ws)
        resolved = self.semantic.resolve(actor, ws, question)
        out: list[dict[str, Any]] = []
        if resolved["ambiguous"]:
            term, names = next(iter(resolved["ambiguous"].items()))
            phrase = _phrase(question, term)
            for n in names:
                m = metrics.get(n)
                if not m:
                    continue
                out.append({"id": f"use:{n}", "kind": "ask", "label": f"I mean {_low(m.title)}",
                            "detail": m.description or f"The approved metric {m.title}.",
                            "question": _reword(question, phrase, m.title)})
            # a mapping only settles it for a sharper phrase ("gross margin"): the bare term stays on both metrics
            if phrase != term and rank >= RANK["admin"]:
                for n in names:
                    if m := metrics.get(n):
                        out.append({"id": f"map:{phrase}:{n}", "kind": "map",
                                    "label": f"Always read “{phrase}” as {_low(m.title)}",
                                    "detail": "Adds it to the glossary for everyone, then asks again.",
                                    "confirm": f"From now on, every request in this workspace that says “{phrase}” is "
                                               f"read as {m.title} ({n}). You can change the mapping in Brain › "
                                               "Definitions; earlier answers keep what they used.",
                                    "term": phrase, "metric": n})
            elif phrase != term and rank >= RANK["editor"]:
                for n in names:
                    if m := metrics.get(n):
                        out.append({"id": f"request:{phrase}:{n}", "kind": "request",
                                    "label": f"Ask an admin to map “{phrase}” to {_low(m.title)}",
                                    "detail": "Nothing changes until an admin approves it in Needs you.",
                                    "term": phrase, "metric": n})
            if rank >= RANK["editor"] and phrase != term and self._admins(ws, actor.user_id):
                out.append({"id": f"define:{phrase}", "kind": "request",
                            "label": f"Neither: ask an admin to define {phrase}",
                            "detail": f"{phrase.capitalize()} becomes its own approved metric once an admin defines it.",
                            "term": phrase})
            return f"Which “{phrase}” do you mean?", out
        if answer.startswith("Name the dataset to profile"):
            for d in self.data.catalog(actor, ws)[:5]:
                out.append({"id": f"dataset:{d['name']}", "kind": "ask", "label": d.get("title") or d["name"],
                            "detail": f"{d.get('row_count') or 0:,} rows" if d.get("row_count") is not None else "",
                            "question": f"Profile {d['name']}"})
            return "Which dataset should Lyst look at?", out
        if answer.startswith("No approved metric matches"):
            want = _words(question)
            scored = sorted(metrics.values(), key=lambda m: (-len(want & _words(
                " ".join([m.title, m.name.replace("_", " "), m.description, *m.synonyms]))), m.title))
            for m in scored[:3]:
                out.append({"id": f"use:{m.name}", "kind": "ask", "label": f"Use {_low(m.title)}",
                            "detail": m.description or f"The approved metric {m.title}.",
                            "question": f"{question.rstrip(' ?.')} (as {_low(m.title)})?"})
            if rank >= RANK["editor"] and self._admins(ws, actor.user_id):
                out.append({"id": "define:", "kind": "request", "label": "Ask an admin to define the metric I mean",
                            "detail": "An admin is asked to add it; nothing changes until they do."})
            return "Which metric should Lyst use?", out
        drift = re.search(r"approved definition of (.+?) needs column", answer)
        if drift:
            name = next((n for n, m in metrics.items() if m.title.lower() == drift.group(1)), None)
            if name:
                out.append({"id": f"open:metric:{name}", "kind": "open", "label": f"Open the {drift.group(1)} definition",
                            "detail": "See which column it needs and what the data has now.",
                            "go": {"page": "app_pages/brain.py", "params": {"metric": name}}})
            return "The data changed under an approved metric", out
        if "Work › Approvals" in answer or any("Approvals" in a for a in r.get("actions") or []):
            out.append({"id": "open:approvals", "kind": "open", "label": "Open what is waiting for approval",
                        "detail": "Once it is approved, ask again and Lyst runs it.",
                        "go": {"page": "app_pages/brain.py", "params": {"view": "work", "work": "needs you"}}})
            out.append({"id": "retry", "kind": "ask", "label": "It's approved: ask again", "question": question,
                        "detail": "Runs the same request now."})
            return "This waits on an approval", out
        if "cannot establish causation" in answer:
            named = [m for m in metrics.values() if m.name in resolved["metrics"]][:2]
            if len(named) == 2:
                out.append({"id": "correlate", "kind": "ask",
                            "label": f"Show how {_low(named[0].title)} and {_low(named[1].title)} move together",
                            "detail": "A correlation, clearly labelled as not causation.",
                            "question": f"Correlation between {_low(named[0].title)} and {_low(named[1].title)}"})
            return "Lyst can't show cause from this request alone", out
        if "role or above" in answer:
            return "This needs someone with more access", out
        return "Lyst needs one more detail", out

    def _unverified(self, question: str, r: dict[str, Any]) -> tuple[str, list[dict]]:
        out: list[dict[str, Any]] = []
        failed = [c for c in (r.get("confidence") or {}).get("checks") or [] if not c.get("passed", True)]
        ref = r.get("open") or {}
        sources = (r.get("supporting") or {}).get("sources") or []
        names = {c.get("name") for c in failed}
        dataset = ref.get("id") if ref.get("kind") == "quality" else (
            sources[0].get("dataset") if names & {"duplicates", "nulls"} and len(sources) == 1 else None)
        if dataset:  # the data quality review (PR #196) is where flagged rows are resolved
            out.append({"id": f"open:quality:{dataset}", "kind": "open", "recommended": True,
                        "label": "Review the flagged rows with Lyst",
                        "detail": "Lyst proposes a fix for each row; nothing changes until it is approved.",
                        "go": {"page": "app_pages/data.py", "params": {"dataset": dataset, "review": "quality"}}})
        out.append({"id": "retry", "kind": "ask", "label": "Run it again on the latest data", "question": question,
                    "detail": "Worth it once the data or the definition has been fixed."})
        out.append({"id": "dismiss:keep", "kind": "dismiss", "label": "Keep it, knowing it isn't verified",
                    "detail": "Clears it from what needs you; the answer stays marked Not verified."})
        what = ", ".join(c.get("name", "a check") for c in failed[:3])
        return (f"{len(failed)} check{'s' if len(failed) != 1 else ''} did not pass ({what})" if failed
                else "This answer did not pass verification"), out

    # -- resolving ----------------------------------------------------------------------------------------------

    def resolve(self, actor: Principal, task_id: str, *, choice: str | None = None, text: str | None = None,
                confirm: bool = False) -> dict[str, Any]:
        """Carry out one choice (or the person's own words). Returns what happened: ``task_id`` of the new answer,
        ``review_id`` of a request, or ``go`` for a place to open. ``open`` choices do not resolve the item."""
        v = self.view(actor, task_id)
        t_ws = self.runtime.get(actor, task_id)["workspace_id"]
        if not v["needs"]:
            raise ValueError("this answer does not need anyone")
        if v["resolution"]:
            raise ValueError(f"already resolved: {v['resolution']['label']}")
        if not v["can_resolve"]:
            raise AccessDenied("only the person who asked, or an editor, can resolve this")
        if text is not None:
            text = text.strip()[:500]
            if not text:
                raise ValueError("say what you mean")
            q = self._clarify(actor, t_ws, v["question"], text)
            new = self.runtime.submit(actor, t_ws, q, extra={"resolves": task_id, "said": text[:200]})
            self._record(actor, t_ws, task_id, "other", "other", text, text=text, follow=new)
            return {"resolved": True, "task_id": new, "question": q, "done": "Asked again with what you said"}
        c = next((c for c in v["choices"] if c["id"] == choice), None)
        if not c:
            raise ValueError("that choice is not offered for this answer")
        if c["kind"] == "open":
            return {"resolved": False, "go": c["go"], "done": c["label"]}
        if c["kind"] == "dismiss":
            self._record(actor, t_ws, task_id, c["id"], "dismiss", c["label"])
            return {"resolved": True, "done": c["label"]}
        if c["kind"] == "ask":
            new = self.runtime.submit(actor, t_ws, c["question"], extra={"resolves": task_id, "choice": c["label"]})
            self._record(actor, t_ws, task_id, c["id"], "ask", c["label"], follow=new)
            return {"resolved": True, "task_id": new, "question": c["question"], "done": c["label"]}
        if c["kind"] == "map":
            if not confirm:
                raise ValueError("this changes the glossary for everyone; confirm it first")
            m = self.semantic.get(actor, t_ws, c["metric"])
            self.semantic.set_term(actor, t_ws, c["term"], f"Means {_low(m.title)}.", c["metric"],
                                   source=f"needs you: {task_id}")
            new = self.runtime.submit(actor, t_ws, v["question"], extra={"resolves": task_id, "choice": c["label"]})
            self._record(actor, t_ws, task_id, c["id"], "map", c["label"], follow=new)
            return {"resolved": True, "task_id": new, "question": v["question"], "done": c["label"]}
        if c["kind"] == "request":
            if self.collab is None:
                raise ValueError("reviews are not available here")
            if c.get("metric"):
                m = self.semantic.get(actor, t_ws, c["metric"])
                rv = self.collab.propose(actor, t_ws, "glossary.map", object_kind="metric", object_id=c["metric"],
                                         payload={"term": c["term"], "maps_to_metric": c["metric"],
                                                  "definition": f"Means {_low(m.title)}."},
                                         summary=f"Read “{c['term']}” as {_low(m.title)} (asked from “{v['question'][:120]}”)")
            else:
                admins = self._admins(t_ws, actor.user_id)
                if not admins:
                    raise ValueError("there is no other admin to ask")
                what = c.get("term") or "the metric this request means"
                rv = self.collab.propose(actor, t_ws, "look", object_kind="task", object_id=task_id, payload={"term": what},
                                         summary=f"Define {what} as an approved metric (asked from “{v['question'][:120]}”)",
                                         reviewer=admins[0])
            self._record(actor, t_ws, task_id, c["id"], "request", c["label"], review=rv["id"])
            return {"resolved": True, "review_id": rv["id"], "done": "Asked an admin. It shows in their Needs you."}
        raise ValueError(f"unknown choice kind {c['kind']}")

    def _clarify(self, actor: Principal, ws: str, question: str, text: str) -> str:
        """Words a person wrote instead of picking: when they name exactly one approved metric for an ambiguous
        term, the request is reworded to it; a whole new question runs as written; anything else is added to the
        request as a clarification (and Lyst asks again if it still cannot tell)."""
        amb = self.semantic.resolve(actor, ws, question)["ambiguous"]
        said = self.semantic.resolve(actor, ws, text)
        if amb:
            term, names = next(iter(amb.items()))
            # "operating margin", or just "the operating one": the one candidate the words point to
            hits = [n for n in names if n in said["metrics"]] or [
                n for n in names if _words(text) & (_words(n.replace("_", " ")) - _words(term))]
            if len(hits) == 1:
                return _reword(question, _phrase(question, term), self.semantic.get(actor, ws, hits[0]).title)
        if text.endswith("?") or len(text.split()) >= 6 and said["metrics"]:
            return text
        return f"{question.rstrip(' ?.')}: {text}"

    def _record(self, actor: Principal, ws: str, task_id: str, choice: str, kind: str, label: str, *,
                text: str | None = None, follow: str | None = None, review: str | None = None) -> None:
        def tx(c):
            if c.execute("SELECT 1 FROM task_resolutions WHERE task_id = ?", [task_id]).fetchone():
                raise ValueError("already resolved")
            c.execute("INSERT INTO task_resolutions (task_id, workspace_id, choice_id, kind, label, text, follow_task_id, "
                      "review_id, resolved_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [task_id, ws, choice[:200], kind, label[:300], text, follow, review, actor.user_id])

        self.stores.control.write(tx)
        self.guard.audit(actor, ws, "task.resolve", task_id, "ok", {"choice": choice[:200], "kind": kind,
                                                                     "follow_task_id": follow, "review_id": review})


__all__ = ["KINDS", "NeedsService"]
