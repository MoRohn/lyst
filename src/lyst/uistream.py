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
a pending approval on the task         a ``lyst_approval`` tool part with ``tool-approval-request``, so any client
                                       answers it with ``addToolApprovalResponse``; the answer is applied only by
                                       ``runtime.decide`` under its role checks, never by the stream or a model
                                       (also ``data-lyst-approval``; ``POST /v1/approvals/{id}`` still works)
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


APPROVAL_TOOL = "lyst_approval"


def approval_call_id(approval_id: str) -> str:
    return f"approval:{approval_id}"


def approval_responses(messages: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    """The answers a client gave to Lyst's approvals with ``addToolApprovalResponse``: ``(approval id, approved)`` for
    each ``lyst_approval`` tool part in ``approval-responded`` state in the last assistant message. They are only
    requests: the API decides each through ``runtime.decide``, under its role checks, as the person who sent them."""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return []
        if m.get("role") != "assistant":
            continue
        out = []
        for p in m.get("parts") or []:
            approval = p.get("approval") if isinstance(p.get("approval"), dict) else {}
            if (p.get("type") == "dynamic-tool" and p.get("toolName") == APPROVAL_TOOL and p.get("state") == "approval-responded"
                    and isinstance(approval.get("approved"), bool) and approval.get("id")):
                out.append((str(approval["id"]), approval["approved"]))
        return out
    return []


def _step_id(task_id: str, step: dict[str, Any]) -> str:
    return f"{task_id}:{step.get('seq')}"


def _step_title(step: dict[str, Any]) -> str:
    """What a person reads for a step: the capability, then what it worked on ("metric value · revenue · Q3 2026")."""
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    period = args.get("period")
    bits = [str(step.get("capability") or step.get("phase") or "step").replace("_", " "), args.get("metric"),
            args.get("dimension") or args.get("by"), period.get("label") if isinstance(period, dict) else None]
    return " · ".join(str(b) for b in bits if isinstance(b, str | int) and str(b).strip())


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

    def continues(self, message: dict[str, Any] | None) -> TaskStream:
        """Continue a message the client already shows (after ``addToolApprovalResponse``, ``useChat`` sends its
        messages and reads the answer into its last assistant message): what that message holds is not sent again, so
        no step or approval appears twice. Only a message for this task is taken; anything else is ignored."""
        if not message or message.get("id") != self.task_id:
            return self
        self.started = True
        for p in message.get("parts") or []:
            cid, state = str(p.get("toolCallId") or ""), p.get("state")
            if p.get("type") != "dynamic-tool" or not cid:
                continue
            if cid.startswith("approval:"):
                self._approvals[cid[len("approval:"):]] = {"output-available": "approved", "output-denied": "rejected"}.get(
                    state, "pending")
            elif cid.startswith(f"{self.task_id}:"):
                self._steps[cid] = {"output-available": "done", "output-error": "failed"}.get(state, "running")
        return self

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
                     "title": _step_title(step)}]
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
        """An approval as the AI SDK's own tool approval (``lyst_approval``, see :func:`approval_responses`), and as
        ``data-lyst-approval`` for clients that read Lyst's data parts."""
        aid, status = str(ap.get("id")), str(ap.get("status") or "pending")
        before = self._approvals.get(aid)
        if before == status:
            return []
        self._approvals[aid] = status
        call = approval_call_id(aid)
        out: list[dict[str, Any]] = []
        if before is None:
            out += [{"type": "tool-input-available", "toolCallId": call, "toolName": APPROVAL_TOOL, "dynamic": True,
                     "title": str(ap.get("summary") or ap.get("action") or "Approval"),
                     "input": {"approvalId": aid, "action": ap.get("action"), "summary": ap.get("summary")}},
                    {"type": "tool-approval-request", "approvalId": aid, "toolCallId": call}]
        if status in ("approved", "rejected"):
            out.append({"type": "tool-approval-response", "approvalId": aid, "approved": status == "approved"})
            out.append({"type": "tool-output-available", "toolCallId": call, "dynamic": True,
                        "output": {"status": status, "decidedBy": ap.get("decided_by")}} if status == "approved"
                       else {"type": "tool-output-denied", "toolCallId": call})
        elif status != "pending":  # expired, or withdrawn with its task
            out.append({"type": "tool-output-error", "toolCallId": call, "dynamic": True,
                        "errorText": f"This approval is {status}."})
        out.append({"type": "data-lyst-approval", "id": f"approval:{aid}",
                    "data": {"approvalId": aid, "action": ap.get("action"), "summary": ap.get("summary"),
                             "status": status, "decide": f"/v1/approvals/{aid}"}})
        return out

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


class AppStream:
    """A Lyst App being generated (``lyst.apps``), as a UI message stream. Pure: it turns the generator's events into
    chunks.

    The app is one dynamic tool call, ``lyst_app``, whose input is the spec. Its ``tool-input-delta`` text is written
    here from checked blocks only (never the model's raw text), so a client's partial JSON is always a checked prefix:
    ``useChat`` shows the spec growing in ``part.input`` while ``state`` is ``input-streaming``. Each checked block
    also arrives with its resolved values as ``data-lyst-app-block``, so the app can draw block by block. A block that
    failed a check is reported as ``data-lyst-app-dropped``. When the model is retried, or its spec is replaced by
    the rules', ``reset-step`` removes what was shown and a new call starts. The end: ``tool-input-available`` with
    the whole spec, ``tool-output-available`` with the saved app, a ``source-document`` per dataset version read.
    """

    TOOL = "lyst_app"

    def __init__(self, gen_id: str) -> None:
        self.gen_id = gen_id
        self.attempt = 0
        self._open = False
        self._blocks = 0
        self._dropped = 0

    @property
    def call_id(self) -> str:
        return f"{self.gen_id}:app:{self.attempt}"

    def _begin(self) -> list[dict[str, Any]]:
        self.attempt += 1
        self._open, self._blocks = False, 0
        return [{"type": "start-step"},
                {"type": "tool-input-start", "toolCallId": self.call_id, "toolName": self.TOOL, "dynamic": True,
                 "title": "Lyst app"}]

    def start(self) -> list[dict[str, Any]]:
        return [{"type": "start", "messageId": self.gen_id, "messageMetadata": {"kind": "lyst-app"}}, *self._begin()]

    def _delta(self, text: str) -> dict[str, Any]:
        return {"type": "tool-input-delta", "toolCallId": self.call_id, "inputTextDelta": text}

    def event(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        kind = ev["type"]
        if kind == "reset":
            return [{"type": "reset-step"}, *self._begin()]
        if kind == "title":
            if self._open:  # a title written after the blocks is in the final input
                return []
            self._open = True
            head = {"title": ev["title"], **({"subtitle": ev["subtitle"]} if ev.get("subtitle") else {})}
            return [self._delta(json.dumps(head)[:-1] + ', "blocks": [')]
        if kind == "block":
            out = []
            if not self._open:
                self._open = True
                out.append(self._delta('{"blocks": ['))
            b = ev["block"]
            out.append(self._delta((", " if self._blocks else "") + json.dumps(b)))
            self._blocks += 1
            out.append({"type": "data-lyst-app-block", "id": f"{self.call_id}:{b['id']}",
                        "data": {"block": b, "data": ev.get("data") or {}}})
            return out
        if kind == "update":  # the same data part again: the client replaces it in place
            b = ev["block"]
            return [{"type": "data-lyst-app-block", "id": f"{self.call_id}:{b['id']}",
                     "data": {"block": b, "data": ev.get("data") or {}}}]
        if kind == "dropped":
            self._dropped += 1
            return [{"type": "data-lyst-app-dropped", "id": f"{self.gen_id}:dropped:{self._dropped}",
                     "data": {"block": ev.get("block"), "reason": ev.get("reason")}}]
        return []

    def finish(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        app = result.get("app") or {}
        out = [self._delta("]}")] if self._open else []
        out += [{"type": "tool-input-available", "toolCallId": self.call_id, "toolName": self.TOOL, "dynamic": True,
                 "input": result["spec"], "title": result["spec"]["title"]},
                {"type": "tool-output-available", "toolCallId": self.call_id, "dynamic": True,
                 "output": {"appId": app.get("app_id"), "version": app.get("version"), "saved": bool(app),
                            "source": result.get("source"), "dropped": result.get("dropped") or [],
                            "sources": result.get("sources") or {}}}]
        for ds, ver in sorted(((result.get("sources") or {}).get("datasets") or {}).items()):
            out.append({"type": "source-document", "sourceId": f"{ds}@{ver}", "mediaType": DATASET_MEDIA_TYPE,
                        "title": f"{ds} v{ver}"})
        meta = {"kind": "lyst-app", "appId": app.get("app_id"), "source": result.get("source")}
        out += [{"type": "finish-step"},
                {"type": "finish", "finishReason": "stop", "messageMetadata": {k: v for k, v in meta.items() if v}}]
        return out

    def fail(self, text: str) -> list[dict[str, Any]]:
        return [{"type": "tool-input-error", "toolCallId": self.call_id, "toolName": self.TOOL, "dynamic": True,
                 "input": {}, "errorText": text},
                {"type": "finish-step"}, {"type": "error", "errorText": text}, {"type": "finish", "finishReason": "error"}]
