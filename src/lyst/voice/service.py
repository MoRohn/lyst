# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice sessions: settings, turns, read-back, retention and the duplex stream ticket.

Privacy defaults: audio is ephemeral. It is transcribed and dropped; it is stored only when a workspace administrator
turns on ``retain_audio``, and then only for ``transcript_days``. Transcripts are redacted (secrets, card numbers,
e-mail addresses) before they are stored, and pruned after ``transcript_days``. External audio engines are off until
an administrator allows them, and are used only while the workspace also allows external models. A computed answer
is spoken by an external engine only when governance would let that answer go to an external model.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from typing import Any

from ..security import AccessDenied, Guard, Principal, redact
from ..store import Stores
from .coordinator import REPLIES, STYLES, classify, speakable
from .engines import (
    PERSONAPLEX_VOICES,
    BrowserEngine,
    EngineError,
    FakeEngine,
    OpenRouterSpeaker,
    OpenRouterTranscriber,
    PersonaPlexEngine,
)
from .gateway import AudioGateway, NoEngine

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "stt_order": ["browser", "openrouter_stt"],
    "tts_order": ["browser"],
    "duplex_order": ["personaplex"],
    "voice": "NATF2",
    "allow_external_audio": False,
    "retain_audio": False,
    "transcript_days": 30,
    "speak_results": True,
}
PREF_DEFAULTS: dict[str, Any] = {"speak_results": None, "mode": "push_to_talk", "voice": None, "style": "professional"}
MODES = ("push_to_talk", "hands_free")
TICKET_SECONDS = 60
IDLE_SESSION_SECONDS = 30 * 60


class VoiceService:
    def __init__(self, stores: Stores, guard: Guard, *, runtime, llm=None, governance=None, gateway: AudioGateway | None = None) -> None:
        self.stores = stores
        self.guard = guard
        self.runtime = runtime
        self.llm = llm
        self.governance = governance
        if gateway is None:
            engines = [BrowserEngine(), PersonaPlexEngine(os.environ.get("LYST_PERSONAPLEX_URL") or None),
                       OpenRouterTranscriber(llm, model=os.environ.get("LYST_VOICE_STT_MODEL") or None),
                       OpenRouterSpeaker(llm, model=os.environ.get("LYST_VOICE_TTS_MODEL") or None,
                                         voice=os.environ.get("LYST_VOICE_TTS_VOICE") or None)]
            if os.environ.get("LYST_VOICE_FAKE") == "1":
                engines.append(FakeEngine())
            gateway = AudioGateway({e.name: e for e in engines})
        self.gateway = gateway
        self._tickets: dict[str, tuple[str, str, str, float]] = {}
        self._lock = threading.Lock()

    def _db(self, ws: str):
        return self.stores.workspace(ws)

    # -- settings ------------------------------------------------------------------------------------------------
    def settings(self, actor: Principal | None, ws: str) -> dict[str, Any]:
        if actor is not None:
            self.guard.require(actor, ws, "voice.use")
        row = self._db(ws).query("SELECT spec FROM voice_settings WHERE id = 'default'")
        return DEFAULTS | (json.loads(row[0]["spec"]) if row else {})

    def set_settings(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.guard.require(actor, ws, "voice.admin", "voice")
        cur = self.settings(None, ws)
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown voice settings: {', '.join(sorted(unknown))}")
        for k in ("stt_order", "tts_order", "duplex_order"):
            if k in changes:
                order = [str(x) for x in changes[k]]
                if bad := [x for x in order if x not in self.gateway.engines]:
                    raise ValueError(f"unknown voice engines: {', '.join(bad)}")
                changes[k] = list(dict.fromkeys(order))
        if "voice" in changes and changes["voice"] not in PERSONAPLEX_VOICES:
            raise ValueError("unknown voice")
        if "transcript_days" in changes:
            days = int(changes["transcript_days"])
            if not 0 <= days <= 400:
                raise ValueError("transcript_days must be between 0 and 400")
            changes["transcript_days"] = days
        for k in ("enabled", "allow_external_audio", "retain_audio", "speak_results"):
            if k in changes:
                changes[k] = bool(changes[k])
        new = cur | changes
        self._db(ws).execute("INSERT OR REPLACE INTO voice_settings (id, spec, updated_by, updated_at) VALUES ('default', ?, ?, now())",
                             [json.dumps({k: new[k] for k in DEFAULTS}), actor.user_id])
        self.guard.audit(actor, ws, "voice.settings", "voice", "ok", {"changed": sorted(changes)})
        return new

    def preferences(self, actor: Principal, ws: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "voice.use")
        row = self._db(ws).query("SELECT spec FROM voice_prefs WHERE user_id = ?", [actor.user_id])
        return PREF_DEFAULTS | (json.loads(row[0]["spec"]) if row else {})

    def set_preferences(self, actor: Principal, ws: str, changes: dict[str, Any]) -> dict[str, Any]:
        cur = self.preferences(actor, ws)
        if unknown := set(changes) - set(PREF_DEFAULTS):
            raise ValueError(f"unknown voice preferences: {', '.join(sorted(unknown))}")
        if changes.get("mode") is not None and changes["mode"] not in MODES:
            raise ValueError("mode is push_to_talk or hands_free")
        if changes.get("voice") is not None and changes["voice"] not in PERSONAPLEX_VOICES:
            raise ValueError("unknown voice")
        if changes.get("style") is not None and changes["style"] not in STYLES:
            raise ValueError(f"style is one of {', '.join(STYLES)}")
        new = cur | changes
        self._db(ws).execute("INSERT OR REPLACE INTO voice_prefs (user_id, spec, updated_at) VALUES (?, ?, now())",
                             [actor.user_id, json.dumps(new)])
        return new

    def engines(self, actor: Principal, ws: str) -> dict[str, Any]:
        cfg = self.settings(actor, ws)
        external_ok = self._external_ok(ws, cfg)
        return {"engines": self.gateway.status(), "external_audio_allowed": external_ok,
                "order": {k: cfg[k] for k in ("stt_order", "tts_order", "duplex_order")},
                "audio_retained": cfg["retain_audio"]}

    def _external_ok(self, ws: str, cfg: dict[str, Any]) -> bool:
        return bool(cfg["allow_external_audio"] and self.llm is not None and self.llm.allowed_for(ws))

    # -- sessions ------------------------------------------------------------------------------------------------
    def start(self, actor: Principal, ws: str, *, mode: str | None = None) -> dict[str, Any]:
        self.guard.require(actor, ws, "voice.use")
        cfg = self.settings(None, ws)
        if not cfg["enabled"]:
            raise AccessDenied("voice is turned off in this workspace")
        mode = mode or self.preferences(actor, ws)["mode"]
        if mode not in MODES:
            raise ValueError("mode is push_to_talk or hands_free")
        sid = "vs_" + uuid.uuid4().hex[:12]
        self._db(ws).execute("INSERT INTO voice_sessions (id, user_id, status, mode, turns) VALUES (?, ?, 'open', ?, 0)",
                             [sid, actor.user_id, mode])
        self.guard.audit(actor, ws, "voice.start", sid, "ok", {"mode": mode})
        return self.session(actor, ws, sid)

    def _own(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "voice.use")
        rows = self._db(ws).query("SELECT * FROM voice_sessions WHERE id = ?", [sid])
        if not rows:
            raise KeyError(sid)
        if rows[0]["user_id"] != actor.user_id:  # a voice session belongs to the person who opened it, admins included
            self.guard.audit(actor, ws, "voice.session", sid, "denied")
            raise AccessDenied("this voice session belongs to someone else")
        return rows[0]

    def session(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        s = self._own(actor, ws, sid)
        turns = self._db(ws).query("SELECT id, seq, role, source, text, intent, task_id, outcome, engines, audio IS NOT NULL AS has_audio, "
                                   "created_at FROM voice_turns WHERE session_id = ? ORDER BY seq", [sid])
        for t in turns:
            t["engines"] = json.loads(t["engines"]) if t["engines"] else []
        return {"id": s["id"], "status": s["status"], "mode": s["mode"], "started_at": s["started_at"], "ended_at": s["ended_at"],
                "turns": turns}

    def end(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        self._own(actor, ws, sid)
        self._db(ws).execute("UPDATE voice_sessions SET status = 'ended', ended_at = now() WHERE id = ? AND status = 'open'", [sid])
        with self._lock:
            for k in [k for k, v in self._tickets.items() if v[2] == sid]:
                self._tickets.pop(k)
        self.guard.audit(actor, ws, "voice.end", sid, "ok")
        return self.session(actor, ws, sid)

    def _record(self, ws: str, sid: str, *, role: str, source: str, text: str, intent: str | None = None,
                task_id: str | None = None, outcome: str | None = None, engines: list | None = None,
                audio: bytes | None = None, mime: str | None = None) -> tuple[str, int]:
        tid = "vt_" + uuid.uuid4().hex[:12]
        db = self._db(ws)

        def tx(c):
            seq = c.execute("SELECT coalesce(max(seq), 0) + 1 FROM voice_turns WHERE session_id = ?", [sid]).fetchone()[0]
            c.execute("INSERT INTO voice_turns (id, session_id, seq, role, source, text, intent, task_id, outcome, engines, audio, audio_mime) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [tid, sid, seq, role, source, redact(text)[:4000], intent, task_id, outcome, json.dumps(engines or []), audio, mime])
            c.execute("UPDATE voice_sessions SET turns = turns + 1, last_activity = now(), "
                      "last_task_id = coalesce(?, last_task_id) WHERE id = ?", [task_id, sid])
            return seq
        return tid, db.write(tx)

    # -- a turn --------------------------------------------------------------------------------------------------
    def turn(self, actor: Principal, ws: str, sid: str, *, text: str | None = None, audio: bytes | None = None,
             mime: str = "audio/wav") -> dict[str, Any]:
        """One utterance: audio (transcribed through the engine order) or text the person's device already
        recognised. Returns what Lyst understood, what it did, and what to say back."""
        s = self._own(actor, ws, sid)
        if s["status"] != "open":
            raise ValueError("this voice session has ended")
        cfg = self.settings(None, ws)
        attempts: list = []
        if audio is not None:
            order = [e for e in cfg["stt_order"] if e != "browser"]  # the browser sends text, not audio
            try:
                tr, attempts = self.gateway.transcribe(audio, mime=mime, workspace=ws, order=order,
                                                       allow_external=self._external_ok(ws, cfg))
            except NoEngine as exc:
                self.guard.audit(actor, ws, "voice.turn", sid, "error", {"reason": "no_engine"})
                return {"session_id": sid, "understood": None, "intent": "unavailable", "engines": exc.attempts,
                        "reply": {"text": "Voice recognition isn't available right now. You can type the question instead.",
                                  "speak": True}}
            except EngineError as exc:
                raise ValueError(str(exc)) from exc
            heard, source = tr.text, f"audio:{tr.engine}"
        elif text is not None:
            heard, source = text, "device_text"
        else:
            raise ValueError("send audio or text")
        keep_audio = audio if (audio is not None and cfg["retain_audio"]) else None
        intent = classify(heard)
        out: dict[str, Any] = {"session_id": sid, "understood": intent.text, "intent": intent.kind, "engines": attempts}
        task_id = None
        if intent.kind == "question":
            # the same call as a typed question: task.create permission, data policy, budgets, admission
            turn_key = "vq_" + uuid.uuid4().hex[:12]
            task_id = self.runtime.submit(actor, ws, intent.text, idempotency_key=f"voice:{sid}:{turn_key}")
            reply = REPLIES["question"]
            out["task_id"] = task_id
        elif intent.kind == "stop_speaking":
            # stops Lyst talking (the page stops playback); the analysis keeps running
            reply = REPLIES["stop_speaking_running"] if self._running_task(actor, ws, s) else REPLIES["stop_speaking"]
            out["stop_playback"] = True
        elif intent.kind == "cancel_work":
            running = self._running_task(actor, ws, s)
            if running:
                self.runtime.cancel(actor, running)  # the person's own cancel permission, as from the Work page
                reply = REPLIES["cancel_running"]
                out["cancelled_task_id"] = running
            else:
                reply = REPLIES["cancel_idle"]
            out["stop_playback"] = True
        elif intent.kind == "catch_up":
            reply = self._catch_up(actor, ws, sid)
        elif intent.kind == "repeat":
            last = self._last_answer(actor, ws, sid)
            reply = last or REPLIES["repeat_none"]
        elif intent.kind == "status":
            reply = self._status_line(actor, ws, s)
        elif intent.kind == "action":
            reply = REPLIES["action"].format(verb=intent.verb)
            out["confirm_on_screen"] = {"verb": intent.verb, "request": intent.text}
            self.guard.audit(actor, ws, "voice.action_refused", sid, "ok", {"verb": intent.verb})
        elif intent.kind == "assent":
            reply = REPLIES["assent"]
            self.guard.audit(actor, ws, "voice.assent_refused", sid, "ok")
        else:
            reply = REPLIES["silence"]
        out["turn_id"], out["seq"] = self._record(ws, sid, role="person", source=source, text=heard, intent=intent.kind, task_id=task_id,
                                      engines=attempts, audio=keep_audio, mime=mime if keep_audio else None)
        self._record(ws, sid, role="lyst", source="coordinator", text=reply, intent=intent.kind, task_id=task_id)
        out["reply"] = {"text": reply, "speak": bool(reply) and self._speaks(actor, ws, cfg)}
        self.guard.audit(actor, ws, "voice.turn", sid, "ok", {"intent": intent.kind, "source": source.split(":")[0]})
        return out

    def _speaks(self, actor: Principal, ws: str, cfg: dict[str, Any]) -> bool:
        pref = self.preferences(actor, ws)["speak_results"]
        return bool(cfg["speak_results"] if pref is None else pref)

    def _running_task(self, actor: Principal, ws: str, s: dict[str, Any]) -> str | None:
        if not s["last_task_id"]:
            return None
        t = self.runtime.get(actor, s["last_task_id"])
        return t["id"] if t["status"] in ("queued", "running", "paused") else None

    def _status_line(self, actor: Principal, ws: str, s: dict[str, Any]) -> str:
        if not s["last_task_id"]:
            return REPLIES["status_none"]
        t = self.runtime.get(actor, s["last_task_id"])
        if t["status"] in ("queued", "running"):
            done = sum(1 for x in t["steps"] if x["status"] == "done")
            return f"Still working: {done} of {len(t['steps']) or 'several'} steps done."
        if t["status"] == "done":
            return "It's finished. Say 'repeat' to hear the answer again."
        return f"The last request is {t['status'].replace('_', ' ')}. Details are on screen."

    def _last_answer(self, actor: Principal, ws: str, sid: str) -> str | None:
        row = self._db(ws).query("SELECT task_id FROM voice_turns WHERE session_id = ? AND task_id IS NOT NULL ORDER BY seq DESC LIMIT 1", [sid])
        if not row:
            return None
        return self.answer(actor, ws, sid, row[0]["task_id"]).get("speak_text") or None

    def answer(self, actor: Principal, ws: str, sid: str, task_id: str) -> dict[str, Any]:
        """The read-back for a question asked in this session: only Lyst's own verified answer is ever spoken."""
        self._own(actor, ws, sid)
        if not self._db(ws).scalar("SELECT count(*) FROM voice_turns WHERE session_id = ? AND task_id = ?", [sid, task_id]):
            raise KeyError(task_id)
        t = self.runtime.get(actor, task_id)
        # a later question in the same session supersedes this one's read-back: the page shows it, but does not
        # start speaking an answer the person has already moved on from
        asked = self._db(ws).scalar("SELECT max(seq) FROM voice_turns WHERE session_id = ? AND task_id = ?", [sid, task_id])
        later = self._db(ws).scalar("SELECT count(*) FROM voice_turns WHERE session_id = ? AND role = 'person' AND "
                                    "intent = 'question' AND seq > ?", [sid, asked])
        if t["status"] in ("queued", "running", "paused"):
            return {"task_id": task_id, "status": t["status"], "ready": False, "superseded": bool(later)}
        limit = STYLES[self.preferences(actor, ws)["style"]]["limit"]
        result = t.get("result") or {}
        if t["status"] == "done" and t.get("outcome") == "complete" and result.get("answer"):
            text = speakable(result["answer"], limit)
        elif t["status"] == "done":
            text = "I have a result, but it needs your attention before I read it out. It's on screen."
        else:
            text = f"That request {t['status'].replace('_', ' ')}. The details are on screen."
        return {"task_id": task_id, "status": t["status"], "outcome": t.get("outcome"), "ready": True, "speak_text": text,
                "answer": result.get("answer"), "superseded": bool(later)}

    def _catch_up(self, actor: Principal, ws: str, sid: str) -> str:
        """What finished for this person since their previous voice session ended (or the last day), and what waits on
        them. Only finished work with a verified answer is summarised; nothing is inferred."""
        since = self._db(ws).scalar("SELECT max(ended_at) FROM voice_sessions WHERE user_id = ? AND id <> ? AND ended_at IS NOT NULL",
                                    [actor.user_id, sid])
        rows = self.stores.control.query(
            "SELECT id, title FROM tasks WHERE workspace_id = ? AND created_by = ? AND status = 'done' AND "
            "updated_at > coalesce(CAST(? AS TIMESTAMPTZ), now() - INTERVAL 1 DAY) ORDER BY updated_at DESC LIMIT 3",
            [ws, actor.user_id, since])
        pending = len(self.runtime.approvals(actor, ws)) if self.guard.can(actor, ws, "approval.decide") else 0
        if not rows and not pending:
            return REPLIES["catch_up_none"]
        parts = []
        for r in rows:
            t = self.runtime.get(actor, r["id"])
            if t.get("outcome") == "complete" and (t.get("result") or {}).get("answer"):
                parts.append(speakable(t["result"]["answer"], 220))
            else:
                parts.append(f"{r['title']} finished and needs your review on screen.")
        if pending:
            parts.append(f"{pending} approval{'s' if pending != 1 else ''} {'are' if pending != 1 else 'is'} waiting for you on screen.")
        return " ".join(parts)

    def speak(self, actor: Principal, ws: str, sid: str, *, text: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        """Speech for a reply. With ``task_id``, the verified answer; an external engine is used only when
        governance would let that answer go to an external model. The browser engine returns the text for the
        page to speak."""
        self._own(actor, ws, sid)
        cfg = self.settings(None, ws)
        if task_id:
            got = self.answer(actor, ws, sid, task_id)
            if not got["ready"]:
                return {"ready": False, "task_id": task_id}
            text = got["speak_text"]
        if not text:
            raise ValueError("nothing to speak")
        allow_external = self._external_ok(ws, cfg)
        if allow_external and task_id and self.governance is not None and self.governance.model_gate(ws, task_id):
            allow_external = False  # the answer was computed from data above the external ceiling
        prefs = self.preferences(actor, ws)
        order = cfg["tts_order"]
        # engines before "browser" in the order speak on the server; the browser speaks in the page
        server = order[: order.index("browser")] if "browser" in order else order
        attempts: list = []
        if server:
            try:
                sp, attempts = self.gateway.speak(text, voice=prefs["voice"] or cfg["voice"], workspace=ws, order=server,
                                                  allow_external=allow_external)
            except NoEngine as exc:
                attempts = exc.attempts
            else:
                import base64  # noqa: PLC0415

                return {"ready": True, "engine": sp.engine, "text": text, "mime": sp.mime,
                        "audio": base64.b64encode(sp.audio).decode(), "engines": attempts}
        if "browser" in order:
            return {"ready": True, "engine": "browser", "text": text, "engines": attempts + [{"engine": "browser", "outcome": "ok"}]}
        return {"ready": True, "engine": None, "text": text, "engines": attempts}

    # -- duplex stream ticket ------------------------------------------------------------------------------------
    def stream_ticket(self, actor: Principal, ws: str, sid: str) -> dict[str, Any]:
        """A one-time, 60-second ticket to open the duplex stream: a browser websocket cannot send the bearer header."""
        s = self._own(actor, ws, sid)
        if s["status"] != "open":
            raise ValueError("this voice session has ended")
        ticket = "vk_" + secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._tickets.items() if v[3] < now]:
                self._tickets.pop(k)
            self._tickets[ticket] = (actor.user_id, ws, sid, now + TICKET_SECONDS)
        return {"ticket": ticket, "expires_in": TICKET_SECONDS}

    def redeem(self, ticket: str, ws: str, sid: str) -> str:
        """The user id a ticket was issued to; the ticket is spent whether or not it matches."""
        with self._lock:
            got = self._tickets.pop(ticket or "", None)
        if not got or got[1] != ws or got[2] != sid or got[3] < time.monotonic():
            raise AccessDenied("the voice stream ticket is missing, used or expired")
        return got[0]

    # -- retention -----------------------------------------------------------------------------------------------
    def prune(self, ws: str) -> dict[str, int]:
        cfg = self.settings(None, ws)
        db = self._db(ws)
        days = int(cfg["transcript_days"])
        old = db.scalar("SELECT count(*) FROM voice_turns WHERE created_at < now() - to_days(CAST(? AS BIGINT))", [days]) or 0
        if old:
            db.execute("DELETE FROM voice_turns WHERE created_at < now() - to_days(CAST(? AS BIGINT))", [days])
        audio = 0
        if not cfg["retain_audio"]:
            audio = db.scalar("SELECT count(*) FROM voice_turns WHERE audio IS NOT NULL") or 0
            if audio:
                db.execute("UPDATE voice_turns SET audio = NULL, audio_mime = NULL WHERE audio IS NOT NULL")
        idle = db.scalar("SELECT count(*) FROM voice_sessions WHERE status = 'open' AND "
                         "coalesce(last_activity, started_at) < now() - to_seconds(CAST(? AS BIGINT))", [IDLE_SESSION_SECONDS]) or 0
        if idle:
            db.execute("UPDATE voice_sessions SET status = 'ended', ended_at = now() WHERE status = 'open' AND "
                       "coalesce(last_activity, started_at) < now() - to_seconds(CAST(? AS BIGINT))", [IDLE_SESSION_SECONDS])
        return {"turns": int(old), "audio": int(audio), "sessions_closed": int(idle)}

    def prune_all(self) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_prune", -1e9) < 600:  # the worker ticks often; retention runs every ten minutes
            return
        self._last_prune = now
        for w in self.stores.control.query("SELECT id FROM workspaces"):
            try:
                self.prune(w["id"])
            except Exception:  # noqa: BLE001 - retention never stops the worker
                import logging  # noqa: PLC0415

                logging.getLogger("lyst.voice").exception("voice retention failed for %s", w["id"])
