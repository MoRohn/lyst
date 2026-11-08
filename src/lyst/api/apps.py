# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Lyst Apps routes (``lyst.apps``): generate an app as an AI SDK UI message stream, save, open and retire apps.

``POST /v1/workspaces/{ws}/apps/generate``
    Takes what ``useChat`` sends (``{id, messages, trigger, messageId}``); the last user message's text is the
    request. Answers a UI message stream (``lyst.uistream.AppStream``): the app's spec grows as the ``lyst_app`` tool's
    input, each checked block arrives with its values as ``data-lyst-app-block``, and the saved app is the tool's
    output. A retried request saves no second app; ``regenerate`` makes a new one.

``GET /v1/workspaces/{ws}/apps/catalog``
    The block types and the metrics an app can bind to, for a person or a client building a spec by hand.

``GET|POST /v1/workspaces/{ws}/apps``, ``GET|PUT /v1/workspaces/{ws}/apps/{id}``, ``GET .../{id}/open``,
``POST .../{id}/retire``
    List and save (editor and above), read a version, open (every value read now), retire.
"""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

import asyncio
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import apps as apps_mod
from .. import uistream
from .aisdk import ChatIn


class SpecIn(BaseModel):
    spec: dict[str, Any]
    note: str = Field("", max_length=300)


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/apps/catalog")
    def apps_catalog(request: Request, ws: str, user: User):
        svc = L(request).apps
        svc.guard.require(user, ws, "data.read")
        return {"blocks": {k: {"binds": list(v["binds"]), "options": v["options"], "does": v["does"]}
                           for k, v in apps_mod.CATALOG.items()},
                "max_blocks": apps_mod.MAX_BLOCKS, "metrics": svc.inventory(svc._metrics(user, ws))}

    @app.post("/v1/workspaces/{ws}/apps/generate")
    async def apps_generate(request: Request, ws: str, body: ChatIn, user: User):
        lyst = L(request)
        text, message_id = uistream.question_from(body.messages)
        if not text:
            raise HTTPException(422, "the last user message has no text")
        regenerate = (body.trigger or "").startswith("regenerate")
        prompt_id = None if regenerate else f"appgen:{body.id}:{body.messageId or message_id or ''}"[:200]
        # checked before the stream opens, so a refusal is a plain 403
        lyst.guard.require(user, ws, "analyst.ask")
        lyst.guard.require(user, ws, "data.query")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stream = uistream.AppStream("ag_" + uuid.uuid4().hex[:12])

        def put(item) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def work() -> None:
            try:
                put(("result", lyst.apps.generate(user, ws, text, emit=lambda ev: put(("event", ev)), prompt_id=prompt_id)))
            except Exception as exc:  # noqa: BLE001 - reported in the stream, which is already open
                put(("error", str(exc)[:500] or type(exc).__name__))

        async def gen():
            for c in stream.start():
                yield uistream.sse(c)
            worker = loop.run_in_executor(None, work)
            while True:
                kind, item = await queue.get()
                if kind == "event":
                    for c in stream.event(item):
                        yield uistream.sse(c)
                    continue
                for c in (stream.finish(item) if kind == "result" else stream.fail(item)):
                    yield uistream.sse(c)
                break
            await worker
            yield uistream.DONE

        return StreamingResponse(gen(), media_type=uistream.MEDIA_TYPE, headers=uistream.HEADERS)

    @app.get("/v1/workspaces/{ws}/apps")
    def apps_list(request: Request, ws: str, user: User, include_retired: bool = False):
        return L(request).apps.list(user, ws, include_retired=include_retired)

    @app.post("/v1/workspaces/{ws}/apps")
    def apps_save(request: Request, ws: str, body: SpecIn, user: User):
        return L(request).apps.save(user, ws, body.spec, note=body.note)

    @app.get("/v1/workspaces/{ws}/apps/{app_id}")
    def apps_get(request: Request, ws: str, app_id: str, user: User, version: int | None = None):
        return L(request).apps.get(user, ws, app_id, version=version)

    @app.put("/v1/workspaces/{ws}/apps/{app_id}")
    def apps_update(request: Request, ws: str, app_id: str, body: SpecIn, user: User):
        return L(request).apps.save(user, ws, body.spec, app_id=app_id, note=body.note)

    @app.get("/v1/workspaces/{ws}/apps/{app_id}/open")
    def apps_open(request: Request, ws: str, app_id: str, user: User):
        return L(request).apps.open(user, ws, app_id)

    @app.post("/v1/workspaces/{ws}/apps/{app_id}/retire")
    def apps_retire(request: Request, ws: str, app_id: str, user: User):
        return L(request).apps.retire(user, ws, app_id)
