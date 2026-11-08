# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Live drafts: the wording a model is writing for a task right now, for the people watching that task.

The analyst's writing steps (``deliver``, ``converse``) stream the model's reply through a guard
(:class:`DraftGuard`) that passes on only text whose figures are already backed by a tool-produced value. What
it passes on is kept here, per task, in this process's memory. The UI message stream (``lyst.uistream``, through
``api/aisdk.py``) reads it and shows the answer forming. The task's stored answer still decides what the answer *is*:
when the task ends, the stream keeps the draft only if it is exactly the stored answer, and otherwise removes it and
shows the stored one.

Nothing here is durable or authoritative: a draft is never stored, never an event and never evidence. It lives for
``TTL`` seconds after its last change, is visible only through ``runtime.get``'s permission check on the same task,
and with workers in another process the stream simply has no draft and shows the answer when the task ends.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

TTL = 900.0
MAX_CHARS = 8000  # a draft longer than any answer is cut, never grown without bound


class Drafts:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._clock = clock

    def _sweep(self, now: float) -> None:
        for k in [k for k, v in self._items.items() if now - v["at"] > TTL]:
            del self._items[k]

    def set(self, task_id: str, text: str) -> None:
        """The text passed on so far. Text that does not continue the previous text starts a new epoch (the stream
        then removes what it showed and starts again)."""
        now = self._clock()
        with self._lock:
            self._sweep(now)
            d = self._items.get(task_id)
            text = text[:MAX_CHARS]
            if d is None:
                self._items[task_id] = {"epoch": 0, "text": text, "state": "writing", "at": now}
            elif d["text"] != text:
                epoch = d["epoch"] if text.startswith(d["text"]) else d["epoch"] + 1
                d.update(epoch=epoch, text=text, state="writing", at=now)

    def close(self, task_id: str) -> None:
        """The model finished (or the guard stopped passing text on). The stored answer decides what stays."""
        with self._lock:
            if d := self._items.get(task_id):
                d.update(state="written", at=self._clock())

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._items.get(task_id)
            return {k: d[k] for k in ("epoch", "text", "state")} if d else None

    def clear(self, task_id: str) -> None:
        with self._lock:
            self._items.pop(task_id, None)


DRAFTS = Drafts()

HOLD_TOKENS = 3  # a figure is judged only once this many words follow it ("3.5 percentage points", "2.69 million")


class DraftGuard:
    """``on_delta`` for a model call whose figures must be backed: it passes on text only while every figure in it is.

    ``figures`` finds the figures in text (``(start, end, value)`` for each), ``backed(value)`` says whether a value
    matches a tool-produced one. Text is passed on up to a word boundary at least ``HOLD_TOKENS`` words behind the
    newest text, so a figure is judged with its unit and scale. The first figure that is not backed stops the draft
    for good: nothing after it is shown, and the analyst's own check on the whole reply decides what happens to it.
    ``on_delta(None)`` (the gateway retrying or moving to the next model) starts over."""

    def __init__(self, task_id: str | None, figures: Callable[[str], list[tuple[int, int, float]]],
                 backed: Callable[[float], bool], drafts: Drafts = DRAFTS) -> None:
        self.task_id = task_id
        self.figures = figures
        self.backed = backed
        self.drafts = drafts
        self.buf = ""
        self.shown = ""
        self.stopped = False

    def __call__(self, piece: str | None) -> None:
        if not self.task_id:
            return
        if piece is None:
            self.buf, self.stopped = "", False
            if self.shown:
                self.shown = ""
                self.drafts.set(self.task_id, "")
            return
        self.buf += piece
        self._advance()

    def _advance(self) -> None:
        if self.stopped:
            return
        starts = [m.start() for m in re.finditer(r"\S+", self.buf)]
        if len(starts) <= HOLD_TOKENS:
            return
        cut = starts[-HOLD_TOKENS]
        for start, end, value in self.figures(self.buf):
            if start >= cut:
                break
            if end > cut:  # a figure that runs into the held-back words is judged later
                cut = start
                break
            if not self.backed(value):
                cut, self.stopped = start, True
                break
        text = self.buf[:cut].rstrip()
        if len(text) > len(self.shown) and text.startswith(self.shown):
            self.shown = text
            self.drafts.set(self.task_id, text)

    def finish(self, final: str | None) -> None:
        """The reply is complete. ``final`` is the wording the analyst kept (its own check passed), or ``None`` when it
        kept its computed answer instead: then the draft is withdrawn at once rather than when the task ends."""
        if not self.task_id:
            return
        if final is None:
            if self.shown:
                self.shown = ""
                self.drafts.set(self.task_id, "")
                self.drafts.close(self.task_id)
            return
        self.shown = final
        self.drafts.set(self.task_id, final)
        self.drafts.close(self.task_id)
