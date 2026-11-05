# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Events lane routes: the workspace event stream as Server-Sent Events with replay, one event with its causal chain
and processing state, integration events posted in, and a workspace's dead letters. Registered from api/__init__.py.

The stream (``GET /v1/workspaces/{ws}/events/stream``)
    ``text/event-stream``; each message is ``id: <seq>``, ``event: <type>``, ``data: <envelope JSON>``. A client that
    reconnects sends ``Last-Event-ID`` (browsers do this themselves) or ``after`` and receives every event it missed,
    in order, filtered to what this member may see; the cursor only moves forward, so a client applies an event only
    when its ``seq`` is above the last one it applied. Membership is checked again on every batch: a member removed
    mid-stream stops receiving at once. A comment line every ``HEARTBEAT`` seconds keeps proxies from closing an idle
    stream, and a stream ends after ``max_seconds`` (at most 15 minutes) so connections are recycled; the client
    reconnects with its cursor and loses nothing. At most ``limit`` events are sent per batch (backpressure: a slow
    reader holds back its own stream, never the engine).
"""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

import json
import time
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..security import AccessDenied

HEARTBEAT = 15.0


class SubjectIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    id: str = Field(min_length=1, max_length=200)


class RuleIn(BaseModel):
    spec: dict[str, Any]


class SimulateIn(BaseModel):
    spec: dict[str, Any]
    events: list[dict[str, Any]] | None = None
    last: int = Field(500, ge=1, le=5000)


class ExternalEventIn(BaseModel):
    type: str = Field(min_length=3, max_length=120, description="external.<system>.<what>")
    subject: SubjectIn
    data: dict[str, Any] = {}
    dedupe_key: str | None = Field(None, max_length=400)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/events/stream")
    async def event_stream(request: Request, ws: str, user: User, after: int | None = None,
                           types: Annotated[list[str] | None, Query()] = None, max_seconds: float = 300,
                           limit: int = 100, last_event_id: Annotated[str | None, Header()] = None):
        lyst = L(request)
        await run_in_threadpool(lyst.guard.require, user, ws, "workspace.read")  # 403 before the stream opens
        pats = [p for t in (types or []) for p in t.split(",") if p] or None
        cursor = after if after is not None else (int(last_event_id) if (last_event_id or "").isdigit() else -1)
        if cursor < 0:  # no cursor: start from now
            cursor = (await run_in_threadpool(lyst.events.list, user, ws, after=-1))["next"]
        span = min(max(max_seconds, 1.0), 900.0)
        batch = min(max(limit, 1), 500)

        async def gen():
            nonlocal cursor
            end = time.monotonic() + span
            yield f"retry: 3000\n: stream from {cursor}\n\n"
            last_beat = time.monotonic()
            while time.monotonic() < end:
                if await request.is_disconnected():
                    return
                try:
                    page = await run_in_threadpool(lyst.events.list, user, ws, after=cursor, types=pats, limit=batch,
                                                   wait=min(HEARTBEAT, max(0.0, end - time.monotonic())))
                except AccessDenied:
                    yield "event: denied\ndata: {}\n\n"
                    return
                for ev in page["events"]:
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
                if page["next"] > cursor:
                    cursor = page["next"]
                if not page["events"] and time.monotonic() - last_beat >= HEARTBEAT - 0.5:
                    last_beat = time.monotonic()
                    yield f": keep-alive {cursor}\n\n"
            yield f"event: end\ndata: {json.dumps({'next': cursor})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/v1/workspaces/{ws}/events/{seq}")
    def event_get(request: Request, ws: str, seq: int, user: User) -> dict[str, Any]:
        """The event, every event in its causal chain that this member may see, and each durable consumer's outcome."""
        return L(request).events.get(user, ws, seq)

    @app.post("/v1/workspaces/{ws}/events", status_code=201)
    def event_post(request: Request, ws: str, body: ExternalEventIn, user: User) -> dict[str, Any]:
        """An integration's event (admins). Repeats with the same ``dedupe_key`` return the first event."""
        return L(request).events.external(user, ws, body.type, subject=(body.subject.kind, body.subject.id),
                                          data=body.data, dedupe_key=body.dedupe_key, sensitivity=body.sensitivity)

    @app.get("/v1/workspaces/{ws}/event-dead-letters")
    def event_dead_letters(request: Request, ws: str, user: User, limit: int = 100) -> list[dict[str, Any]]:
        return L(request).events.dead_letters(user, ws, limit=limit)

    @app.post("/v1/workspaces/{ws}/event-dead-letters/{consumer}/{seq}/retry")
    def event_dead_letter_retry(request: Request, ws: str, consumer: str, seq: int, user: User) -> dict[str, Any]:
        return L(request).events.retry_dead(user, ws, consumer, seq)

    # -- rules and activities -----------------------------------------------------------------

    @app.get("/v1/rule-templates")
    def rule_templates(user: User) -> dict[str, Any]:
        from ..autonomy import ACTIONS, LEVELS, TEMPLATES  # noqa: PLC0415

        return {"templates": TEMPLATES, "levels": list(LEVELS),
                "actions": {k: {"risk": v[0], "runs_from": v[1], "permission": v[2]} for k, v in ACTIONS.items()}}

    @app.get("/v1/workspaces/{ws}/rules")
    def rules_list(request: Request, ws: str, user: User) -> list[dict[str, Any]]:
        return L(request).autonomy.rules(user, ws)

    @app.post("/v1/workspaces/{ws}/rules", status_code=201)
    def rules_create(request: Request, ws: str, body: RuleIn, user: User) -> dict[str, Any]:
        return L(request).autonomy.save(user, ws, body.spec)

    @app.put("/v1/workspaces/{ws}/rules/{rid}")
    def rules_update(request: Request, ws: str, rid: str, body: RuleIn, user: User) -> dict[str, Any]:
        """A new version; an active rule returns to draft until activated again."""
        return L(request).autonomy.save(user, ws, body.spec, rule_id=rid)

    @app.post("/v1/workspaces/{ws}/rules/simulate")
    def rules_simulate(request: Request, ws: str, body: SimulateIn, user: User) -> dict[str, Any]:
        """Dry run: what the rule would have done over recent (or given) events. Nothing is run or recorded."""
        return L(request).autonomy.simulate(user, ws, body.spec, events=body.events, last=body.last)

    @app.post("/v1/workspaces/{ws}/rules/{rid}/activate")
    def rules_activate(request: Request, ws: str, rid: str, user: User) -> dict[str, Any]:
        return L(request).autonomy.activate(user, ws, rid)

    @app.post("/v1/workspaces/{ws}/rules/{rid}/deactivate")
    def rules_deactivate(request: Request, ws: str, rid: str, user: User, retire: bool = False) -> dict[str, Any]:
        return L(request).autonomy.deactivate(user, ws, rid, retire=retire)

    @app.get("/v1/workspaces/{ws}/rules/{rid}/evaluations")
    def rules_evaluations(request: Request, ws: str, rid: str, user: User, limit: int = 100) -> list[dict[str, Any]]:
        return L(request).autonomy.evaluations(user, ws, rid, limit=limit)

    @app.get("/v1/workspaces/{ws}/activities")
    def activities_list(request: Request, ws: str, user: User, status: str | None = None, kind: str | None = None,
                        id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:  # noqa: A002 - query name
        return L(request).autonomy.activities(user, ws, status=status, subject=(kind, id) if kind and id else None, limit=limit)

    @app.get("/v1/workspaces/{ws}/activities/{aid}")
    def activities_get(request: Request, ws: str, aid: str, user: User) -> dict[str, Any]:
        """The activity, its triggering event and its flow: trigger, rule, decision, task, verification, artifact."""
        return L(request).autonomy.get(user, ws, aid)

    @app.post("/v1/workspaces/{ws}/activities/{aid}/run")
    def activities_run(request: Request, ws: str, aid: str, user: User) -> dict[str, Any]:
        return L(request).autonomy.run(user, ws, aid)

    @app.post("/v1/workspaces/{ws}/activities/{aid}/dismiss")
    def activities_dismiss(request: Request, ws: str, aid: str, user: User) -> dict[str, Any]:
        return L(request).autonomy.dismiss(user, ws, aid)
