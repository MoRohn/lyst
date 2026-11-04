# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Voice: spoken questions into the same governed analyst, verified answers read back.

engines.py      speech engines (browser, PersonaPlex duplex, OpenRouter speech-to-text, a test engine), with honest support labels
gateway.py      engine choice, capacity, circuit breaking and fallback
coordinator.py  what a turn means; speech never authorises anything
service.py      settings, sessions, turns, read-back, retention, the duplex stream ticket
"""

from .service import VoiceService

__all__ = ["VoiceService"]
