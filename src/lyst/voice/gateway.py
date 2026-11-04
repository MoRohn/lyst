# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The real-time audio gateway: picks an engine for each piece of work, admits it within the engine's capacity, and
falls back down the workspace's order when an engine is unhealthy, busy or refused by policy.

* Capacity is per engine and counts live sessions: the published PersonaPlex server holds one conversation per GPU
  process, so a second duplex request either waits briefly or falls back, it never queues without bound.
* A circuit breaker opens after consecutive failures and stays open for a cool-off, so a dead GPU host costs one
  timeout, not one per utterance. The first call after the cool-off probes it again.
* An external engine is used only when the caller's policy check allows it (``allow_external``); the reason it was
  skipped is kept in the attempt trail, which the turn records.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .engines import Engine, EngineError, Speech, Transcript

MAX_TURN_AUDIO_BYTES = 8 * 1024 * 1024   # one utterance; longer speech is split by the client
MAX_SPEAK_CHARS = 4000


class NoEngine(EngineError):
    """No engine in the order could do the work; ``attempts`` says why each was passed over."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass
class _Health:
    failures: int = 0
    open_until: float = 0.0
    in_use: int = 0
    last_error: str = ""
    served: int = 0
    latencies: list[int] = field(default_factory=list)   # recent measured latencies (ms), for status only


class AudioGateway:
    def __init__(self, engines: dict[str, Engine], *, failure_threshold: int = 3, cool_off: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.engines = engines
        self.failure_threshold = failure_threshold
        self.cool_off = cool_off
        self.clock = clock
        self._health: dict[str, _Health] = {n: _Health() for n in engines}
        self._lock = threading.Lock()

    def register(self, engine: Engine) -> None:
        with self._lock:
            self.engines[engine.name] = engine
            self._health.setdefault(engine.name, _Health())

    # -- health ------------------------------------------------------------------------------------------------
    def _ok(self, name: str, ms: int) -> None:
        with self._lock:
            h = self._health[name]
            h.failures, h.open_until, h.served = 0, 0.0, h.served + 1
            h.latencies = (h.latencies + [ms])[-50:]

    def _fail(self, name: str, error: str) -> None:
        with self._lock:
            h = self._health[name]
            h.failures += 1
            h.last_error = error[:200]
            if h.failures >= self.failure_threshold:
                h.open_until = self.clock() + self.cool_off

    def _skip_reason(self, engine: Engine, capability: str, allow_external: bool, check_state: bool = True) -> str | None:
        if capability not in engine.capabilities:
            return f"does not offer {capability}"
        if not engine.local and not allow_external:
            return "external audio is not allowed here"
        h = self._health[engine.name]
        if h.open_until > self.clock():
            return f"paused after repeated failures ({h.last_error or 'error'})"
        if h.in_use >= engine.capacity:
            return "at capacity"
        if check_state:
            st = engine.status()
            if st.state != "ready":
                return st.reason or st.state
        return None

    # -- work --------------------------------------------------------------------------------------------------
    def _run(self, capability: str, order: list[str], allow_external: bool, call) -> tuple[Any, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        for name in order:
            engine = self.engines.get(name)
            if engine is None:
                attempts.append({"engine": name, "outcome": "skipped", "reason": "not installed"})
                continue
            if why := self._skip_reason(engine, capability, allow_external):
                attempts.append({"engine": name, "outcome": "skipped", "reason": why})
                continue
            with self._lock:
                self._health[name].in_use += 1
            started = self.clock()
            try:
                out = call(engine)
            except EngineError as exc:
                self._fail(name, str(exc))
                attempts.append({"engine": name, "outcome": "failed", "reason": str(exc)[:200]})
                continue
            finally:
                with self._lock:
                    self._health[name].in_use -= 1
            ms = int((self.clock() - started) * 1000)
            self._ok(name, ms)
            attempts.append({"engine": name, "outcome": "ok", "ms": ms})
            return out, attempts
        raise NoEngine(f"no voice engine could {capability}", attempts)

    def transcribe(self, audio: bytes, *, mime: str, workspace: str, order: list[str], allow_external: bool) -> tuple[Transcript, list]:
        if not audio:
            raise EngineError("no audio")
        if len(audio) > MAX_TURN_AUDIO_BYTES:
            raise EngineError(f"an utterance is limited to {MAX_TURN_AUDIO_BYTES // (1024 * 1024)} MB; send shorter pieces")
        return self._run("stt", order, allow_external, lambda e: e.transcribe(audio, mime=mime, workspace=workspace))

    def speak(self, text: str, *, voice: str | None, workspace: str, order: list[str], allow_external: bool) -> tuple[Speech, list]:
        text = text[:MAX_SPEAK_CHARS]
        return self._run("tts", order, allow_external, lambda e: e.speak(text, voice=voice, workspace=workspace))

    @contextmanager
    def duplex(self, order: list[str], *, allow_external: bool = False) -> Iterator[Engine]:
        """Hold a duplex slot for the life of a stream; falls back down the order when the first engine is busy."""
        attempts: list[dict[str, Any]] = []
        chosen = None
        with self._lock:
            for name in order:
                engine = self.engines.get(name)
                if engine is None:
                    attempts.append({"engine": name, "outcome": "skipped", "reason": "not installed"})
                    continue
                if why := self._skip_reason(engine, "duplex", allow_external, check_state=False):
                    attempts.append({"engine": name, "outcome": "skipped", "reason": why})
                    continue
                chosen = engine
                self._health[name].in_use += 1
                break
        if chosen is None:
            raise NoEngine("no duplex voice engine is free", attempts)
        try:
            yield chosen
        finally:
            with self._lock:
                self._health[chosen.name].in_use -= 1

    def report(self, engine: str, ok: bool, error: str = "") -> None:
        """Called by the stream bridge when a duplex connection succeeds or fails."""
        if engine in self._health:
            self._ok(engine, 0) if ok else self._fail(engine, error)

    # -- status ------------------------------------------------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        out = []
        now = self.clock()
        for name, engine in self.engines.items():
            st = engine.status().as_dict()
            h = self._health[name]
            lat = sorted(h.latencies)
            st["health"] = {"in_use": h.in_use, "failures": h.failures, "paused": h.open_until > now,
                            "last_error": h.last_error, "served": h.served,
                            # measured in this process only; None until something has been measured
                            "p50_ms": lat[len(lat) // 2] if lat else None}
            if st["health"]["paused"]:
                st["state"] = "unavailable"
                st["reason"] = f"paused after repeated failures: {h.last_error}"
            out.append(st)
        return out
