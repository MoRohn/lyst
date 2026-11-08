# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst's work as an AI SDK UI message stream: the wire format any AI SDK client (``useChat``, ``Chat``,
``readUIMessageStream``) reads, written by Lyst's own governed engine.

Why Lyst speaks the protocol instead of running the AI SDK on a server: every model call, permission check, budget,
contract and receipt already lives in the Python engine (``llm.gateway``, ``security.Guard``, ``runtime``,
``harness``). A Node server calling models would bypass all of it. The protocol is a small, versioned, documented
format (``x-vercel-ai-ui-message-stream: v1``, Server-Sent Events of JSON chunks, ending ``data: [DONE]``), so the
engine writes it and the AI SDK reads it unchanged. ``tests/aisdk`` holds the proof: the stream written here is
parsed by the real ``ai`` package's own schema and assembled by its own ``readUIMessageStream``.

How a task maps onto a message (:class:`TaskStream`, pure: it diffs successive task snapshots and never reads a
database, so it is tested without one):

=====================================  ==========================================================================
Lyst                                   UI message stream chunks
=====================================  ==========================================================================
the task                               ``start`` (``messageId`` = task id) ... ``finish`` (``finishReason``)
where the task is (lifecycle)          ``data-lyst-status`` with a stable id, so the client updates it in place
each engine step (a capability)        ``start-step``, ``tool-input-available`` (dynamic tool named after the
                                       capability, input = its arguments), then ``tool-output-available`` or
                                       ``tool-output-error``, then ``finish-step``
a pending approval on the task         ``data-lyst-approval`` (decided only through ``POST /v1/approvals/{id}``
                                       by a person with the right role; never by the stream or the model)
the answer as a model writes it        its own step: ``text-start`` / ``text-delta`` as the figure-checked draft
                                       grows (``lyst.live``), then ``text-end`` if it is exactly the stored answer,
                                       else ``reset-step`` (removed) and the stored answer follows
the answer                             ``text-start`` / ``text-delta`` / ``text-end``
each dataset version it read           ``source-document`` (``sourceId`` = ``dataset@version``)
each chart, the confidence, the        ``data-lyst-chart``, message metadata (``messageMetadata``)
outcome and the cost
a failed run / a cancelled one         ``error`` then ``finish`` (``error``) / ``abort``
=====================================  ==========================================================================

Nothing here invents a number: every value in a chunk is copied from the stored task, its steps and its result.
Events never carry row values; this stream carries what the answer page already shows the same person, after the
same permission check (``runtime.get`` requires ``workspace.read``).
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL = "v1"
HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "x-vercel-ai-ui-message-stream": PROTOCOL,
}
MEDIA_TYPE = "text/event-stream"
DONE = "data: [DONE]\n\n"
DATASET_MEDIA_TYPE = "application/vnd.lyst.dataset"

# Engine step status -> tool part outcome. Anything not listed is still running.
_STEP_DONE = {"done", "ok", "succeeded", "skipped"}
_STEP_FAILED = {"failed", "error"}
_TERMINAL = {"done", "failed", "cancelled"}
_OUTPUT_KEYS = ("status", "phase", "agent", "started_at", "finished_at")


def sse(chunk: dict[str, Any]) -> str:
    """One chunk as a Server-Sent Event, the framing the AI SDK's ``parseJsonEventStream`` reads."""
    return f"data: {json.dumps(chunk, default=str, separators=(',', ':'))}\n\n"


def question_from(messages: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """The question in an AI SDK chat request: the text of the last user message and that message's id.

    ``useChat`` sends ``{id, messages: [UIMessage], trigger, messageId}``; a UIMessage carries its text in ``parts``
    (``{"type": "text", "text": ...}``). Only text is read: files and data parts are not questions."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        text = " ".join(p.get("text", "") for p in m.get("parts") or [] if p.get("type") == "text").strip()
        if not text and isinstance(m.get("content"), str):  # older clients
            text = m["content"].strip()
        return (text or None), m.get("id")
    return None, None


def _step_id(task_id: str, step: dict[str, Any]) -> str:
    return f"{task_id}:{step.get('seq')}"


def _step_output(step: dict[str, Any]) -> dict[str, Any]:
    return {k: step[k] for k in _OUTPUT_KEYS if step.get(k) is not None}


def _confidence(result: dict[str, Any]) -> dict[str, Any] | None:
    c = result.get("confidence") or {}
    return {k: c[k] for k in ("level", "score", "reasons") if k in c} or None


class TaskStream:
    """Turns successive snapshots of one task (``runtime.get``) into the chunks that are new since the last one.

    Feed it every snapshot with :meth:`update`; it returns the chunks to send, in order. It remembers what it already
    sent, so a snapshot that changed nothing returns nothing, and :meth:`update` after the task ended returns nothing.
    A fresh instance replays the task from the beginning (what a resumed stream needs)."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.started = False
        self.finished = False
        self._status: str | None = None
        self._steps: dict[str, str] = {}  # step id -> "running" | "done" | "failed"
        self._approvals: dict[str, str] = {}  # approval id -> status
        self._draft: dict[str, Any] | None = None  # the open draft step: {epoch, id, text}
        self._answered = False  # the draft became the answer, so the answer is not sent again

    # -- the pieces ------------------------------------------------------------------------------------------------

    def _start(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        self.started = True
        meta = {"taskId": self.task_id, "workspace": task.get("workspace_id"), "title": task.get("title")}
        return [{"type": "start", "messageId": self.task_id, "messageMetadata": {k: v for k, v in meta.items() if v}}]

    def _lifecycle(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        life = task.get("lifecycle") or {}
        state = life.get("state") or task.get("status")
        label = life.get("label") or life.get("summary")
        key = json.dumps([state, label], default=str)
        if key == self._status:
            return []
        self._status = key
        data = {"state": state, "status": task.get("status")}
        if label:
            data["label"] = label
        return [{"type": "data-lyst-status", "id": f"{self.task_id}:status", "data": data}]

    def _step(self, step: dict[str, Any]) -> list[dict[str, Any]]:
        sid = _step_id(self.task_id, step)
        status = str(step.get("status") or "").lower()
        now = "done" if status in _STEP_DONE else "failed" if status in _STEP_FAILED else "running"
        seen = self._steps.get(sid)
        if seen == now or (seen in ("done", "failed")):
            return []
        out: list[dict[str, Any]] = []
        if seen is None:
            capability = str(step.get("capability") or "step")
            out += [{"type": "start-step"},
                    {"type": "tool-input-available", "toolCallId": sid, "toolName": capability,
                     "input": step.get("args") or {}, "dynamic": True,
                     "title": str(step.get("phase") or capability).replace("_", " ")}]
        if now == "done":
            out += [{"type": "tool-output-available", "toolCallId": sid, "output": _step_output(step), "dynamic": True},
                    {"type": "finish-step"}]
        elif now == "failed":
            out += [{"type": "tool-output-error", "toolCallId": sid, "dynamic": True,
                     "errorText": str(step.get("error") or "This step failed.")},
                    {"type": "finish-step"}]
        self._steps[sid] = now
        return out

    def _approval(self, ap: dict[str, Any]) -> list[dict[str, Any]]:
        aid, status = str(ap.get("id")), str(ap.get("status") or "pending")
        if self._approvals.get(aid) == status:
            return []
        self._approvals[aid] = status
        return [{"type": "data-lyst-approval", "id": f"approval:{aid}",
                 "data": {"approvalId": aid, "action": ap.get("action"), "summary": ap.get("summary"),
                          "status": status, "decide": f"/v1/approvals/{aid}"}}]

    def _drafting(self, draft: dict[str, Any]) -> list[dict[str, Any]]:
        """The answer as the model writes it, in a step of its own (``lyst.live``). A new epoch (the model call was
        retried, or its wording withdrawn) removes what was shown with ``reset-step``; the task's end decides whether
        the draft stays (:meth:`_settle`)."""
        out: list[dict[str, Any]] = []
        epoch, text = draft.get("epoch"), str(draft.get("text") or "")
        if self._draft and self._draft["epoch"] != epoch:
            out.append({"type": "reset-step"})
            self._draft = None
        if self._draft is None and text:
            pid = f"{self.task_id}:draft:{epoch}"
            self._draft = {"epoch": epoch, "id": pid, "text": ""}
            out += [{"type": "start-step"}, {"type": "text-start", "id": pid}]
        if self._draft and len(text) > len(self._draft["text"]) and text.startswith(self._draft["text"]):
            out.append({"type": "text-delta", "id": self._draft["id"], "delta": text[len(self._draft["text"]):]})
            self._draft["text"] = text
        return out

    def _settle(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """At the task's end the stored answer decides: a draft that is exactly the answer stays; any other is removed."""
        if not self._draft:
            return []
        d, self._draft = self._draft, None
        answer = (task.get("result") or {}).get("answer") if task.get("status") == "done" else None
        if isinstance(answer, str) and answer == d["text"]:
            self._answered = True
            return [{"type": "text-end", "id": d["id"]}, {"type": "finish-step"}]
        return [{"type": "reset-step"}]

    def _result(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        r = task.get("result") or {}
        out: list[dict[str, Any]] = []
        answer = r.get("answer")
        if isinstance(answer, str) and answer.strip() and not self._answered:
            tid = f"{self.task_id}:answer"
            out += [{"type": "text-start", "id": tid}, {"type": "text-delta", "id": tid, "delta": answer},
                    {"type": "text-end", "id": tid}]
        seen: set[str] = set()
        for s in (r.get("supporting") or {}).get("sources") or []:
            if not s.get("dataset"):
                continue
            sid = f"{s['dataset']}@{s.get('version')}" if s.get("version") is not None else str(s["dataset"])
            if sid in seen:
                continue
            seen.add(sid)
            title = s["dataset"] + (f" v{s['version']}" if s.get("version") is not None else "")
            out.append({"type": "source-document", "sourceId": sid, "mediaType": DATASET_MEDIA_TYPE, "title": title})
        for i, c in enumerate(r.get("charts") or []):
            out.append({"type": "data-lyst-chart", "id": f"{self.task_id}:chart:{i}", "data": {"index": i, **c}})
        return out

    def _metadata(self, task: dict[str, Any]) -> dict[str, Any]:
        r = task.get("result") or {}
        meta: dict[str, Any] = {"taskId": self.task_id, "status": task.get("status")}
        if task.get("outcome"):
            meta["outcome"] = task["outcome"]
        if conf := _confidence(r):
            meta["confidence"] = conf
        if r.get("mode"):
            meta["mode"] = r["mode"]
        if task.get("cost_usd") is not None:
            meta["costUsd"] = task["cost_usd"]
        return meta

    def _finish(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        self.finished = True
        status = task.get("status")
        if status == "cancelled":
            return [{"type": "abort", "reason": "cancelled"}]
        if status == "failed":
            return [{"type": "error", "errorText": str(task.get("error") or "Lyst could not finish this.")},
                    {"type": "finish", "finishReason": "error", "messageMetadata": self._metadata(task)}]
        return [*self._result(task), {"type": "finish", "finishReason": "stop", "messageMetadata": self._metadata(task)}]

    # -- the diff --------------------------------------------------------------------------------------------------

    def update(self, task: dict[str, Any], approvals: list[dict[str, Any]] | None = None,
               draft: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The chunks that describe what changed since the previous snapshot. ``draft`` is the task's live draft
        (``lyst.live.DRAFTS.get``), if any. While a draft is open, step and approval changes wait (they are diffs, so
        nothing is lost): the draft stays the newest step, which is what lets ``reset-step`` remove exactly it."""
        if self.finished:
            return []
        out = [] if self.started else self._start(task)
        out += self._lifecycle(task)
        terminal = task.get("status") in _TERMINAL
        if draft is not None and (not terminal or self._draft):  # at the end, only catch up a draft already shown
            out += self._drafting(draft)
        if terminal:
            out += self._settle(task)
        if self._draft is None:
            for step in task.get("steps") or []:
                out += self._step(step)
            for ap in approvals or []:
                out += self._approval(ap)
        if terminal:
            out += self._finish(task)
        return out

    def chunks(self, task: dict[str, Any], approvals: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Every chunk for a task in one pass (a finished task, or a test)."""
        return self.update(task, approvals)
