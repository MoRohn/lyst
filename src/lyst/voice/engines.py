# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice engines: what can turn speech into words, words into speech, or hold a full-duplex conversation.

Every engine reports, honestly, how far it has been checked (``support``):

* ``implemented_verified``   - exercised end to end by the test suite;
* ``implemented_unverified`` - the code exists and its pure parts are tested, but it has not run against the real
  service on this installation (no GPU, no key, no model access);
* ``partial`` / ``planned`` / ``unsupported`` - as they say.

Engines never authorise anything. They move audio and text; what the words *do* is decided by the coordinator,
under the same permission checks as a typed request.

PersonaPlex (NVIDIA, MIT code, NVIDIA Open Model License weights, gated on Hugging Face) is a full-duplex
speech-to-speech model built on Moshi. Its published server (``python -m moshi.server``) takes one conversation at a
time on a websocket at ``/api/chat?voice_prompt=..&text_prompt=..&seed=..``; every binary message starts with a kind
byte (0 handshake, 1 audio as Opus, 2 text piece). Lyst never bundles or downloads its weights: an operator runs the
server on a CUDA host and points ``LYST_PERSONAPLEX_URL`` at it.

Two limits of the published server shape how Lyst uses it, and are stated rather than hidden:

* the persona (text prompt) is fixed when the connection opens and nothing can be injected mid-conversation, so
  PersonaPlex cannot be made to *say* a computed answer. Lyst uses it for conversational presence (turn-taking,
  acknowledgement, barge-in) under a persona that forbids stating figures; answers are spoken from Lyst's own
  verified text through a speech engine (or the browser), never improvised by the duplex model;
* it does not return a transcript of the person's speech, only its own text, so the person's words still come
  from a speech-to-text engine (or the browser's own recognition) before Lyst acts on them.
"""

from __future__ import annotations

import base64
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

SUPPORT = ("implemented_verified", "implemented_unverified", "partial", "planned", "unsupported")

# PersonaPlex / Moshi server wire protocol (moshi/server.py): first byte of every binary message
KIND_HANDSHAKE, KIND_AUDIO, KIND_TEXT = 0, 1, 2
PERSONAPLEX_SAMPLE_RATE = 24_000
PERSONAPLEX_VOICES = tuple([f"NATF{i}" for i in range(4)] + [f"NATM{i}" for i in range(4)]
                           + [f"VARF{i}" for i in range(5)] + [f"VARM{i}" for i in range(5)])

# The persona the duplex model runs under: present and courteous, never the source of a figure or a decision.
GROUNDED_PERSONA = (
    "You are the voice of Lyst, an analyst's assistant. Listen, acknowledge briefly and let the person finish. "
    "Never state numbers, dates, names of customers, results or conclusions yourself; say that Lyst is checking and "
    "that the verified answer will follow. Never agree to approve, delete, send, publish or change anything; say "
    "that it needs confirming on screen."
)


class EngineError(RuntimeError):
    """The engine could not do what was asked (unreachable, refused, malformed)."""


@dataclass
class Transcript:
    text: str
    engine: str
    confidence: float | None = None
    language: str | None = None
    latency_ms: int | None = None


@dataclass
class Speech:
    audio: bytes
    mime: str
    engine: str
    latency_ms: int | None = None


@dataclass
class EngineStatus:
    name: str
    title: str
    capabilities: list[str]
    local: bool
    state: str                 # ready | unavailable | unconfigured | disabled
    support: str
    reason: str = ""
    capacity: int = 1          # concurrent sessions the engine serves
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "capabilities": self.capabilities, "local": self.local,
                "state": self.state, "support": self.support, "reason": self.reason, "capacity": self.capacity,
                "detail": self.detail}


class Engine:
    """Base class. Subclasses set the class attributes and implement what their capabilities name."""

    name = "engine"
    title = "Engine"
    capabilities: tuple[str, ...] = ()   # stt | tts | duplex
    local = True
    support = "planned"
    capacity = 1

    def status(self) -> EngineStatus:
        return EngineStatus(self.name, self.title, list(self.capabilities), self.local, "unavailable", self.support,
                            "not implemented", self.capacity)

    def transcribe(self, audio: bytes, *, mime: str, workspace: str) -> Transcript:
        raise EngineError(f"{self.title} does not transcribe")

    def speak(self, text: str, *, voice: str | None, workspace: str) -> Speech:
        raise EngineError(f"{self.title} does not speak")


# -- the fake engine: deterministic, used by tests and by installations with nothing else ------------------------------
class FakeEngine(Engine):
    """A deterministic stand-in. "Audio" is UTF-8 text prefixed with ``FAKE:``; speech is the text, framed the same
    way. It proves the plumbing (sessions, fallback, permissions, retention) without a model; it is never presented
    as speech recognition."""

    name = "fake"
    title = "Test engine (no real speech)"
    capabilities = ("stt", "tts", "duplex")
    local = True
    support = "implemented_verified"
    capacity = 4

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[str] = []

    def status(self) -> EngineStatus:
        return EngineStatus(self.name, self.title, list(self.capabilities), True, "unavailable" if self.fail else "ready",
                            self.support, "set to fail" if self.fail else "", self.capacity)

    def transcribe(self, audio: bytes, *, mime: str, workspace: str) -> Transcript:
        self.calls.append("stt")
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise EngineError("fake engine set to fail")
        if not audio.startswith(b"FAKE:"):
            raise EngineError("the test engine only understands FAKE: audio")
        return Transcript(audio[5:].decode("utf-8", "replace").strip(), self.name, confidence=1.0, language="en")

    def speak(self, text: str, *, voice: str | None, workspace: str) -> Speech:
        self.calls.append("tts")
        if self.fail:
            raise EngineError("fake engine set to fail")
        return Speech(b"FAKE:" + text.encode(), "audio/x-lyst-fake", self.name)

    # duplex: echo-style session used to test the streaming bridge
    def duplex_handshake(self) -> bytes:
        return bytes([KIND_HANDSHAKE])

    def duplex_reply(self, frame: bytes) -> list[bytes]:
        kind, payload = decode_frame(frame)
        if kind != KIND_AUDIO:
            return []
        return [encode_frame(KIND_AUDIO, payload), encode_frame(KIND_TEXT, b" mm-hm")]


# -- PersonaPlex ---------------------------------------------------------------------------------------------------------
def encode_frame(kind: int, payload: bytes = b"") -> bytes:
    if kind not in (KIND_HANDSHAKE, KIND_AUDIO, KIND_TEXT):
        raise ValueError("unknown frame kind")
    return bytes([kind]) + payload


def decode_frame(frame: bytes) -> tuple[int, bytes]:
    if not frame:
        raise ValueError("empty frame")
    return frame[0], frame[1:]


def personaplex_url(base: str, *, voice: str, text_prompt: str = GROUNDED_PERSONA, seed: int | None = None) -> str:
    """The websocket address of one conversation on a PersonaPlex server (``/api/chat`` with its query)."""
    if voice not in PERSONAPLEX_VOICES:
        raise ValueError(f"unknown PersonaPlex voice {voice!r}")
    parts = urlsplit(base)
    if parts.scheme not in ("ws", "wss", "http", "https"):
        raise ValueError("the PersonaPlex address must be ws://, wss://, http:// or https://")
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    query = {"voice_prompt": f"{voice}.pt", "text_prompt": text_prompt}
    if seed is not None:
        query["seed"] = str(int(seed))
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/chat?{urlencode(query)}"


class PersonaPlexEngine(Engine):
    """A PersonaPlex (Moshi) server run by the operator on a CUDA host. Lyst bridges the duplex stream to it; it does
    not transcribe or speak Lyst's answers (see the module notes)."""

    name = "personaplex"
    title = "NVIDIA PersonaPlex (local, full duplex)"
    capabilities = ("duplex",)
    local = True
    support = "implemented_unverified"   # protocol and bridge are tested against a fake; never run against a real GPU here
    capacity = 1                         # the published server holds one conversation at a time (a single lock)

    def __init__(self, url: str | None, *, voice: str = "NATF2", connect_timeout: float = 1.5) -> None:
        self.url = url
        self.voice = voice
        self.connect_timeout = connect_timeout

    def _reachable(self) -> tuple[bool, str]:
        parts = urlsplit(self.url or "")
        host, port = parts.hostname, parts.port or (443 if parts.scheme in ("https", "wss") else 8998)
        if not host:
            return False, "no address"
        try:
            with socket.create_connection((host, port), timeout=self.connect_timeout):
                return True, ""
        except OSError as exc:
            return False, f"{host}:{port} is not answering ({type(exc).__name__})"

    def status(self) -> EngineStatus:
        detail = {"voices": list(PERSONAPLEX_VOICES), "sample_rate": PERSONAPLEX_SAMPLE_RATE, "codec": "opus",
                  "requires": "a Linux host with an NVIDIA Ampere/Hopper-class GPU (tested by NVIDIA on A100 80 GB), "
                              "the gated nvidia/personaplex-7b-v1 weights accepted under the NVIDIA Open Model License",
                  "latency": "not measured on this installation"}
        if not self.url:
            return EngineStatus(self.name, self.title, list(self.capabilities), True, "unconfigured", self.support,
                                "set LYST_PERSONAPLEX_URL to a running PersonaPlex server", self.capacity, detail)
        try:
            import websockets  # noqa: F401, PLC0415
        except ImportError:
            return EngineStatus(self.name, self.title, list(self.capabilities), True, "unavailable", "unsupported",
                                "the websockets package is not installed", self.capacity, detail)
        ok, why = self._reachable()
        return EngineStatus(self.name, self.title, list(self.capabilities), True, "ready" if ok else "unavailable",
                            self.support, why, self.capacity, detail)

    def address(self, *, voice: str | None = None, seed: int | None = None) -> str:
        if not self.url:
            raise EngineError("PersonaPlex is not configured")
        return personaplex_url(self.url, voice=voice or self.voice, seed=seed)


# -- OpenRouter audio (an external provider): dedicated transcription and speech endpoints ----------------------------
_AUDIO_FORMATS = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3",
                  "audio/webm": "webm", "audio/ogg": "ogg", "audio/flac": "flac", "audio/m4a": "m4a", "audio/mp4": "m4a",
                  "audio/aac": "aac"}
OPENROUTER_INLINE_LIMIT = 25 * 1024 * 1024   # documented multipart cap; inline base64 above it is refused by most providers


class _OpenRouterAudio(Engine):
    """Shared plumbing: the key comes from the credential broker at call time (never from the page), every request is
    a row in llm_calls, and an unconfigured model is reported rather than guessed."""

    local = False
    support = "implemented_unverified"   # request and response handling tested against a stub; not run against the live API here
    capacity = 8
    env_model = ""
    endpoint = ""
    purpose = ""

    def __init__(self, llm, *, model: str | None = None, http=None, timeout: float = 60.0) -> None:
        self.llm = llm
        self.model = model
        self.http = http
        self.timeout = timeout

    def _base(self) -> str:
        return (self.llm.settings.base_url or "https://openrouter.ai/api/v1").rstrip("/")

    def status(self) -> EngineStatus:
        base = (self.name, self.title, list(self.capabilities), False)
        if self.llm is None or self.llm.settings.provider != "openrouter":
            return EngineStatus(*base, "unconfigured", self.support, "this installation does not use OpenRouter", self.capacity)
        if not self.model:
            return EngineStatus(*base, "unconfigured", self.support, f"choose a model ({self.env_model})", self.capacity)
        if not self.llm.available:
            return EngineStatus(*base, "unconfigured", self.support, "no OpenRouter key is connected", self.capacity)
        return EngineStatus(*base, "ready", self.support, "", self.capacity, {"model": self.model})

    def _post(self, workspace: str, body: dict[str, Any]):
        import httpx  # noqa: PLC0415

        st = self.status()
        if st.state != "ready":
            raise EngineError(st.reason)
        key = self.llm._key_for(workspace)  # noqa: SLF001 - the credential broker, at call time
        if not key:
            raise EngineError("no OpenRouter key is connected")
        started = time.monotonic()
        client = self.http or httpx.Client()
        try:
            r = client.post(f"{self._base()}{self.endpoint}", json=body, timeout=self.timeout,
                            headers={"Authorization": f"Bearer {key}", "X-Title": "Lyst"})
        except httpx.HTTPError as exc:
            self._record(workspace, None, int((time.monotonic() - started) * 1000), {})
            raise EngineError(f"OpenRouter did not answer ({type(exc).__name__})") from exc
        return r, int((time.monotonic() - started) * 1000)

    def _record(self, workspace: str, status: int | None, ms: int, usage: dict[str, Any]) -> None:
        """Every upstream request is a row in llm_calls, voice included."""
        self.llm.stores.control.execute(
            "INSERT INTO llm_calls (id, workspace_id, model, purpose, external, input_tokens, output_tokens, latency_ms, "
            "outcome, workload, provider, status, cost_usd) VALUES (nextval('llm_call_seq'), ?, ?, ?, true, ?, ?, ?, ?, "
            "'voice', 'openrouter', ?, ?)",
            [workspace, self.model, self.purpose, usage.get("input_tokens"), usage.get("output_tokens"), ms,
             "ok" if status == 200 else "error", status, usage.get("cost")])

    def discover(self, kind: str) -> list[dict[str, Any]]:
        """Models OpenRouter lists for ``speech`` or ``transcription`` right now, with their voices: choices come from
        the live catalogue, not from a list in this file."""
        import httpx  # noqa: PLC0415

        client = self.http or httpx.Client()
        try:
            r = client.get(f"{self._base()}/models", params={"output_modalities": kind}, timeout=15)
            data = r.json().get("data") or [] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError) as exc:
            raise EngineError(f"OpenRouter's model list is unavailable ({type(exc).__name__})") from exc
        return [{"id": m.get("id"), "name": m.get("name"), "voices": m.get("supported_voices") or []} for m in data if m.get("id")]


class OpenRouterTranscriber(_OpenRouterAudio):
    """``POST /audio/transcriptions`` with base64 ``input_audio``."""

    name = "openrouter_stt"
    title = "OpenRouter speech-to-text (external)"
    capabilities = ("stt",)
    env_model = "LYST_VOICE_STT_MODEL"
    endpoint = "/audio/transcriptions"
    purpose = "voice.stt"

    def request(self, audio: bytes, mime: str, *, language: str | None = None) -> dict[str, Any]:
        fmt = _AUDIO_FORMATS.get(mime.split(";")[0].strip().lower())
        if fmt is None:
            raise EngineError(f"unsupported audio type {mime!r}")
        if len(audio) > OPENROUTER_INLINE_LIMIT:
            raise EngineError("audio is over OpenRouter's 25 MB limit; split the recording")
        body: dict[str, Any] = {"model": self.model, "temperature": 0,
                                "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}
        if language:
            body["language"] = language
        return body

    def transcribe(self, audio: bytes, *, mime: str, workspace: str) -> Transcript:
        r, ms = self._post(workspace, self.request(audio, mime, language="en"))
        try:
            data = r.json()
        except ValueError:
            data = {}
        usage = data.get("usage") or {}
        self._record(workspace, r.status_code, ms, usage)
        if r.status_code != 200:
            raise EngineError(f"OpenRouter refused the transcription (HTTP {r.status_code})")
        text = data.get("text")
        if not isinstance(text, str):
            raise EngineError("OpenRouter returned an unreadable transcription")
        return Transcript(text.strip(), self.name, confidence=data.get("confidence"), language=data.get("language"), latency_ms=ms)


class OpenRouterSpeaker(_OpenRouterAudio):
    """``POST /audio/speech``: the body is the audio itself (mp3)."""

    name = "openrouter_tts"
    title = "OpenRouter text-to-speech (external)"
    capabilities = ("tts",)
    env_model = "LYST_VOICE_TTS_MODEL"
    endpoint = "/audio/speech"
    purpose = "voice.tts"

    def __init__(self, llm, *, model: str | None = None, voice: str | None = None, **kw) -> None:
        super().__init__(llm, model=model, **kw)
        self.voice = voice

    def status(self) -> EngineStatus:
        st = super().status()
        if st.state == "ready" and not self.voice:
            st.state, st.reason = "unconfigured", "choose a voice for the model (LYST_VOICE_TTS_VOICE)"
        return st

    def request(self, text: str, *, speed: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "input": text, "voice": self.voice, "response_format": "mp3"}
        if speed and speed != 1.0:
            body["speed"] = float(speed)
        return body

    def speak(self, text: str, *, voice: str | None, workspace: str) -> Speech:
        r, ms = self._post(workspace, self.request(text))
        self._record(workspace, r.status_code, ms, {})
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        if r.status_code != 200:
            raise EngineError(f"OpenRouter refused the speech (HTTP {r.status_code})")
        if not ctype.startswith("audio/") or not r.content:
            raise EngineError("OpenRouter returned no audio")
        return Speech(r.content, ctype, self.name, latency_ms=ms)


class BrowserEngine(Engine):
    """The person's own browser: Web Speech recognition sends text, and the page speaks answers with its own
    synthesis. Nothing leaves for a third party through Lyst (the browser vendor's recognition may, which the
    Settings page states)."""

    name = "browser"
    title = "Browser speech (on the person's device)"
    capabilities = ("stt", "tts")
    local = True
    support = "partial"   # the server side (text turns, speak_text) is verified; the page component belongs to the UI
    capacity = 1000

    def status(self) -> EngineStatus:
        return EngineStatus(self.name, self.title, list(self.capabilities), True, "ready", self.support,
                            "recognition and speech run in the page", self.capacity)
