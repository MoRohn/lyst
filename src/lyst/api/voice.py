# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice routes: settings, engines, sessions, turns, read-back, and the duplex stream. Registered from
api/__init__.py in one block.

The duplex stream is a websocket the page opens with a one-time ticket (a browser websocket cannot send the bearer
header). Frames use PersonaPlex's own framing (first byte 0 handshake, 1 audio, 2 text), so the page speaks one
protocol whether the far end is a PersonaPlex server or the test engine. Text pieces from the duplex model are its
own conversational words, never an answer: answers come from ``/answer`` and are read from Lyst's verified result.
"""

# no `from __future__ import annotations`: FastAPI resolves the User dependency from the real annotation

import asyncio
import base64
import binascii
import logging
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..security import AccessDenied, Principal

log = logging.getLogger("lyst.voice")
MAX_FRAME = 64 * 1024
IDLE_SECONDS = 60


class VoiceSettingsIn(BaseModel):
    enabled: bool | None = None
    stt_order: list[str] | None = None
    tts_order: list[str] | None = None
    duplex_order: list[str] | None = None
    voice: str | None = None
    allow_external_audio: bool | None = None
    retain_audio: bool | None = None
    transcript_days: int | None = None
    speak_results: bool | None = None


class VoicePrefsIn(BaseModel):
    speak_results: bool | None = None
    mode: str | None = None
    voice: str | None = None
    style: str | None = None


class VoiceStartIn(BaseModel):
    mode: str | None = None


class VoiceTurnIn(BaseModel):
    text: str | None = None          # recognised on the person's device
    audio_b64: str | None = None     # one utterance, base64
    mime: str = "audio/wav"
    confidence: float | None = None  # the device recogniser's confidence for ``text``, 0..1


class VoiceSpeakIn(BaseModel):
    task_id: str | None = None
    text: str | None = None


def routes(app: FastAPI, L, User) -> None:  # noqa: N803 - mirrors the names used in api/__init__.py
    @app.get("/v1/workspaces/{ws}/voice/settings")
    def voice_settings(request: Request, ws: str, user: User) -> dict[str, Any]:
        return L(request).voice.settings(user, ws)

    @app.patch("/v1/workspaces/{ws}/voice/settings")
    def voice_settings_set(request: Request, ws: str, body: VoiceSettingsIn, user: User) -> dict[str, Any]:
        return L(request).voice.set_settings(user, ws, body.model_dump(exclude_none=True))

    @app.get("/v1/workspaces/{ws}/voice/preferences")
    def voice_prefs(request: Request, ws: str, user: User) -> dict[str, Any]:
        return L(request).voice.preferences(user, ws)

    @app.put("/v1/workspaces/{ws}/voice/preferences")
    def voice_prefs_set(request: Request, ws: str, body: VoicePrefsIn, user: User) -> dict[str, Any]:
        return L(request).voice.set_preferences(user, ws, body.model_dump(exclude_unset=True))

    @app.get("/v1/workspaces/{ws}/voice/engines")
    def voice_engines(request: Request, ws: str, user: User) -> dict[str, Any]:
        return L(request).voice.engines(user, ws)

    @app.post("/v1/workspaces/{ws}/voice/sessions", status_code=201)
    def voice_start(request: Request, ws: str, body: VoiceStartIn, user: User) -> dict[str, Any]:
        return L(request).voice.start(user, ws, mode=body.mode)

    @app.get("/v1/workspaces/{ws}/voice/sessions/{sid}")
    def voice_session(request: Request, ws: str, sid: str, user: User) -> dict[str, Any]:
        return L(request).voice.session(user, ws, sid)

    @app.post("/v1/workspaces/{ws}/voice/sessions/{sid}/end")
    def voice_end(request: Request, ws: str, sid: str, user: User) -> dict[str, Any]:
        return L(request).voice.end(user, ws, sid)

    @app.post("/v1/workspaces/{ws}/voice/sessions/{sid}/turns")
    def voice_turn(request: Request, ws: str, sid: str, body: VoiceTurnIn, user: User) -> dict[str, Any]:
        audio = None
        if body.audio_b64 is not None:
            try:
                audio = base64.b64decode(body.audio_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("audio_b64 is not valid base64") from exc
        if body.confidence is not None and not 0 <= body.confidence <= 1:
            raise ValueError("confidence is between 0 and 1")
        return L(request).voice.turn(user, ws, sid, text=body.text, audio=audio, mime=body.mime, confidence=body.confidence)

    @app.get("/v1/workspaces/{ws}/voice/sessions/{sid}/answer/{task_id}")
    def voice_answer(request: Request, ws: str, sid: str, task_id: str, user: User) -> dict[str, Any]:
        return L(request).voice.answer(user, ws, sid, task_id)

    @app.post("/v1/workspaces/{ws}/voice/sessions/{sid}/speak")
    def voice_speak(request: Request, ws: str, sid: str, body: VoiceSpeakIn, user: User) -> dict[str, Any]:
        return L(request).voice.speak(user, ws, sid, text=body.text, task_id=body.task_id)

    @app.post("/v1/workspaces/{ws}/voice/sessions/{sid}/stream-ticket")
    def voice_ticket(request: Request, ws: str, sid: str, user: User) -> dict[str, Any]:
        return L(request).voice.stream_ticket(user, ws, sid)

    @app.websocket("/v1/workspaces/{ws}/voice/sessions/{sid}/duplex")
    async def voice_duplex(websocket: WebSocket, ws: str, sid: str, ticket: str = "") -> None:
        lyst = websocket.app.state.lyst
        if lyst is None:
            await websocket.close(code=1013)
            return
        try:
            uid = lyst.voice.redeem(ticket, ws, sid)
            row = lyst.stores.control.query("SELECT id, email FROM users WHERE id = ?", [uid])
            actor = Principal(row[0]["id"], row[0]["email"]) if row else None
            if actor is None:
                raise AccessDenied("unknown user")
            s = lyst.voice.session(actor, ws, sid)  # the person must still be allowed voice in this workspace
            if s["status"] != "open":
                raise AccessDenied("session ended")
        except (AccessDenied, KeyError):
            await websocket.close(code=1008)
            return
        cfg = lyst.voice.settings(None, ws)
        await websocket.accept()
        try:
            with lyst.voice.gateway.duplex(cfg["duplex_order"]) as engine:
                lyst.guard.audit(actor, ws, "voice.duplex", sid, "ok", {"engine": engine.name})
                if engine.name == "personaplex":
                    await _bridge_personaplex(websocket, engine, lyst.voice.gateway, voice=cfg["voice"])
                else:
                    await _bridge_local(websocket, engine)
        except Exception as exc:  # noqa: BLE001 - a stream failure closes the stream, never the server
            from ..voice.gateway import NoEngine  # noqa: PLC0415

            reason = "no duplex engine is free" if isinstance(exc, NoEngine) else "voice stream failed"
            if not isinstance(exc, NoEngine | WebSocketDisconnect):
                log.warning("voice duplex stream failed: %s", type(exc).__name__)
            try:
                await websocket.close(code=1013, reason=reason)
            except RuntimeError:
                pass


async def _bridge_local(websocket: WebSocket, engine) -> None:
    """An in-process duplex engine (the test engine)."""
    await websocket.send_bytes(engine.duplex_handshake())
    while True:
        try:
            frame = await asyncio.wait_for(websocket.receive_bytes(), timeout=IDLE_SECONDS)
        except (TimeoutError, WebSocketDisconnect):
            return
        if len(frame) > MAX_FRAME:
            await websocket.close(code=1009)
            return
        for out in engine.duplex_reply(frame):
            await websocket.send_bytes(out)


async def _bridge_personaplex(websocket: WebSocket, engine, gateway, *, voice: str) -> None:
    """Relay frames between the page and a PersonaPlex server. Only audio frames go up; audio and text come down."""
    import websockets  # noqa: PLC0415

    from ..voice.engines import KIND_AUDIO  # noqa: PLC0415

    try:
        upstream = await asyncio.wait_for(websockets.connect(engine.address(voice=voice), max_size=MAX_FRAME, open_timeout=10), 15)
    except Exception as exc:  # noqa: BLE001
        gateway.report(engine.name, False, f"connect failed ({type(exc).__name__})")
        raise
    gateway.report(engine.name, True)
    async with upstream:
        async def up() -> None:
            while True:
                frame = await asyncio.wait_for(websocket.receive_bytes(), timeout=IDLE_SECONDS)
                if len(frame) > MAX_FRAME:
                    return
                if frame and frame[0] == KIND_AUDIO:
                    await upstream.send(frame)

        async def down() -> None:
            async for msg in upstream:
                if isinstance(msg, bytes) and msg:
                    await websocket.send_bytes(msg)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
