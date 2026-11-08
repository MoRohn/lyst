# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""AI SDK lane routes: ask Lyst from any AI SDK client and watch the work as a UI message stream.

``POST /v1/workspaces/{ws}/chat``
    The request ``useChat`` (``DefaultChatTransport``) sends: ``{id, messages, trigger, messageId}``, plus optional
    ``autonomy``. The text of the last user message is the question; it goes through the same pipeline as
    ``POST /v1/workspaces/{ws}/tasks`` (including "yes" to a conversational reply's offer). The answer is the task,
    streamed as it runs (``lyst.uistream``): header ``x-vercel-ai-ui-message-stream: v1``, ending ``data: [DONE]``.
    A retried request for the same chat and user message returns the same task (idempotency), so a dropped
    connection never runs the work twice.

``POST /v1/workspaces/{ws}/chat/tickets``
    A chat ticket for the signed-in person (``lyst.tickets``): what the interface hands a browser so the AI SDK can
    use the two chat routes for this workspace, and generate or open a Lyst App (``lyst.api.apps``), and nothing else,
    for 15 minutes. Send it as ``Authorization: Ticket <ticket>``. These routes answer cross-origin requests only from the interface's own
    origins (``LYST_UI_ORIGINS``, comma-separated; by default the local interface at ``lyst.localhost``, ``localhost``
    and ``127.0.0.1`` on ``LYST_UI_PORT``).

Approvals in the chat
    A pending approval on the task is a ``lyst_approval`` tool part in ``approval-requested`` state, so any AI SDK
    client answers it with ``addToolApprovalResponse`` (and ``sendAutomaticallyWhen:
    lastAssistantMessageIsCompleteWithApprovalResponses``). The next request carries the answers; each is applied
    through ``runtime.decide`` as the person who sent it, under the same role checks as ``POST /v1/approvals/{id}``,
    and the task's stream continues. A chat ticket cannot decide: a page's credential never approves anything.

``GET /v1/workspaces/{ws}/chat/{chat_id}/stream``
    ``useChat({resume: true})``: the chat's task that is still running, replayed from its start and then live;
    ``204`` when nothing is running, as the AI SDK expects.

The stream reads the task through ``runtime.get``, which checks ``workspace.read`` for the person on every poll;
the task's own record decides everything shown. A stream ends at the task's end or after ``MAX_SECONDS`` (the
client resumes), and a comment line every ``HEARTBEAT`` seconds keeps proxies from closing it.
"""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

import asyncio
import os
import re
import time
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import live, tickets, uistream
from ..analyst import converse as converse_mod
from ..security import AccessDenied, Principal

POLL = 0.4  # seconds between task snapshots
HEARTBEAT = 15.0
MAX_SECONDS = 900.0


class ChatIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    messages: list[dict[str, Any]] = Field(max_length=500)
    trigger: Literal["submit-message", "submit-user-message", "regenerate-message",
                     "regenerate-assistant-message"] | None = None
    messageId: str | None = Field(None, max_length=200)  # noqa: N815 - the AI SDK's field name
    autonomy: str | None = None


# the routes a chat ticket opens, and the only ones that answer the interface cross-origin: the chat, its resume, and
# generating or opening a Lyst App (lyst.apps)
CHAT_PATH = re.compile(r"^/v1/workspaces/[^/]+/(?:chat(?:/[^/]+/stream)?|apps/generate|apps/app_[a-z0-9]+/open)$")


def ui_origins() -> set[str]:
    """The interface's origins, which alone may call the chat routes from a browser."""
    if raw := os.environ.get("LYST_UI_ORIGINS"):
        return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}
    port = os.environ.get("LYST_UI_PORT", "8501")
    return {f"http://{h}:{port}" for h in ("lyst.localhost", "localhost", "127.0.0.1")}


def _cors(origin: str) -> dict[str, str]:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin", "Access-Control-Max-Age": "600",
            "Access-Control-Allow-Methods": "GET, POST", "Access-Control-Allow-Headers": "authorization, content-type",
            "Access-Control-Expose-Headers": "x-vercel-ai-ui-message-stream"}


def _approvals(lyst, task_id: str) -> list[dict[str, Any]]:
    return lyst.runtime.db.query("SELECT id, action, summary, status, decided_by FROM approvals WHERE task_id = ? "
                                 "ORDER BY created_at", [task_id])


def _decide(lyst, user: Principal, ws: str, responses: list[tuple[str, bool]]) -> str | None:
    """Apply a client's answers to Lyst's approvals (``addToolApprovalResponse``) through ``runtime.decide``: its role
    checks, and the rule that a capability's proposer cannot approve it, apply as for ``POST /v1/approvals/{id}``. An
    approval already decided keeps its decision. Returns the task the approvals belong to, to stream on."""
    task_id = None
    for aid, approved in responses:
        rows = lyst.runtime.db.query("SELECT id, task_id, status FROM approvals WHERE id = ? AND workspace_id = ?", [aid, ws])
        if not rows:
            raise HTTPException(404, f"no approval {aid} in this workspace")
        if rows[0]["status"] == "pending":
            lyst.runtime.decide(user, aid, approved)
        task_id = task_id or rows[0]["task_id"]
    return task_id


def _stream(request: Request, lyst, user, task_id: str, shown: dict[str, Any] | None = None) -> StreamingResponse:
    """The task as a UI message stream; ``shown`` is the assistant message the client already has for it, which the
    stream continues instead of replaying."""

    async def gen():
        ts = uistream.TaskStream(task_id).continues(shown)
        end = time.monotonic() + MAX_SECONDS
        beat = time.monotonic()
        while time.monotonic() < end:
            if await request.is_disconnected():
                return
            try:
                task = await run_in_threadpool(lyst.runtime.get, user, task_id)
                approvals = await run_in_threadpool(_approvals, lyst, task_id)
            except (AccessDenied, KeyError):
                yield uistream.sse({"type": "error", "errorText": "This answer is no longer available to you."})
                yield uistream.DONE
                return
            chunks = ts.update(task, approvals, live.DRAFTS.get(task_id))
            for c in chunks:
                yield uistream.sse(c)
            if ts.finished:
                yield uistream.DONE
                return
            if not chunks and time.monotonic() - beat >= HEARTBEAT:
                beat = time.monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(POLL)
        # out of time while the task still runs: end without [DONE]; the client resumes with GET .../stream

    return StreamingResponse(gen(), media_type=uistream.MEDIA_TYPE, headers=uistream.HEADERS)


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.middleware("http")
    async def chat_cors(request: Request, call_next):
        """Cross-origin access to the chat routes, for the interface's origins only. Every other route stays same-origin."""
        origin = request.headers.get("origin", "").rstrip("/")
        if not origin or not CHAT_PATH.match(request.url.path) or origin not in ui_origins():
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_cors(origin))
        response = await call_next(request)
        response.headers.update(_cors(origin))
        return response

    def chat_user(request: Request, ws: str) -> Principal:
        """The person behind a chat request: a session token, or a chat ticket for this workspace (``lyst.tickets``)."""
        auth = request.headers.get("authorization", "")
        if auth[:7].lower() != "ticket ":
            return L(request).guard.authenticate(auth[7:] if auth[:7].lower() == "bearer " else None)
        try:
            uid = tickets.verify(auth[7:].strip(), ws)
        except tickets.TicketInvalid as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": tickets.SCHEME}) from None
        rows = L(request).stores.control.query("SELECT id, email FROM users WHERE id = ?", [uid])
        if not rows:
            raise HTTPException(401, "this chat ticket's person no longer exists")
        return Principal(rows[0]["id"], rows[0]["email"], token_label=tickets.LABEL)

    ChatUser = Annotated[Principal, Depends(chat_user)]  # noqa: N806 - a type alias, as User is
    app.state.chat_user = ChatUser  # the apps routes a ticket also opens (api/apps.py)

    @app.post("/v1/workspaces/{ws}/chat/tickets")
    def chat_ticket(request: Request, ws: str, user: User):
        if user.token_label == tickets.LABEL:  # not reachable with a ticket today; kept explicit so it never becomes so
            raise AccessDenied("a chat ticket cannot issue another")
        L(request).guard.require(user, ws, "analyst.ask")
        return {**tickets.issue(user.user_id, ws), "api": f"/v1/workspaces/{ws}/chat", "apps": f"/v1/workspaces/{ws}/apps"}

    @app.post("/v1/workspaces/{ws}/chat")
    async def chat(request: Request, ws: str, body: ChatIn, user: ChatUser):
        lyst = L(request)
        responses = uistream.approval_responses(body.messages)
        if responses:  # the person answered Lyst's approvals in the chat: decide them, then keep streaming their task
            if user.token_label == tickets.LABEL:
                raise AccessDenied("approvals are decided with a signed-in session, not a chat ticket: "
                                   "use Work › Approvals")
            task_id = await run_in_threadpool(_decide, lyst, user, ws, responses)
            if task_id is None:
                return Response(status_code=204)
            shown = next((m for m in reversed(body.messages) if m.get("role") == "assistant"), None)
            return _stream(request, lyst, user, task_id, shown)
        question, message_id = uistream.question_from(body.messages)
        if not question:
            raise HTTPException(422, "the last user message has no text to ask")
        extra: dict[str, Any] = {"chat_id": body.id, "channel": "ai-sdk"}
        regenerate = (body.trigger or "").startswith("regenerate")
        took = await run_in_threadpool(converse_mod.follow_up, lyst.runtime.db, user, ws, question)
        if took:
            extra |= {"in_reply_to": took["task_id"], "said": question[:200]}
            question = took["question"]
        # one task per user message: a retry of the same request returns it; "regenerate" asks again on purpose
        key = None if regenerate else f"chat:{body.id}:{body.messageId or message_id or ''}"[:200]
        task_id = await run_in_threadpool(lambda: lyst.runtime.submit(user, ws, question, autonomy=body.autonomy,
                                                                      extra=extra, idempotency_key=key))
        return _stream(request, lyst, user, task_id)

    @app.get("/v1/workspaces/{ws}/chat/{chat_id}/stream")
    async def chat_resume(request: Request, ws: str, chat_id: str, user: ChatUser):
        lyst = L(request)
        await run_in_threadpool(lyst.guard.require, user, ws, "workspace.read")
        rows = await run_in_threadpool(
            lyst.runtime.db.query,
            "SELECT id FROM tasks WHERE workspace_id = ? AND created_by = ? AND request->>'$.chat_id' = ? "
            "AND status NOT IN ('done', 'failed', 'cancelled') ORDER BY created_at DESC LIMIT 1",
            [ws, user.user_id, chat_id])
        if not rows:
            return Response(status_code=204)
        return _stream(request, lyst, user, rows[0]["id"])
