# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Wiring for the extensibility lane, attached to the composition root after it is built.

Kept apart from ``Lyst.build`` so the lane adds services without editing the shared constructor: each service
becomes an attribute of the ``Lyst`` instance (``lyst.events``, ``lyst.webhooks`` ...) and registers its hooks here.
"""

from __future__ import annotations


def attach(lyst) -> None:
    from .events import EventService
    from .webhooks import WebhookService

    lyst.events = EventService(lyst.stores, lyst.guard)
    lyst.webhooks = WebhookService(lyst.stores, lyst.guard, lyst.events)
    lyst.runtime.periodic.append(lyst.webhooks.run)  # collects events, then delivers what is due

    from .experiments import ExperimentService

    lyst.experiments = ExperimentService(lyst.stores, lyst.guard, lyst.data, lyst.semantic, lambda: lyst)

    from .workbench import WorkbenchService

    lyst.workbench = WorkbenchService(lyst.stores, lyst.guard, lyst.data, lyst.knowledge, lyst.settings.home)

    from .portability import PortabilityService

    lyst.portability = PortabilityService(lyst.stores, lyst.guard, lyst.credentials, lyst.settings.home)
    lyst.portability.governance = getattr(lyst, 'governance', None)
    # -- other lanes
    from .specialists.core import UniversalAnalystCore  # noqa: PLC0415 - specializations lane

    lyst.analyst_core = UniversalAnalystCore(lyst)
    if getattr(lyst, "anticipation", None) is not None:  # anticipation looks again soon after relevant events
        lyst.events.subscribers.append(lyst.anticipation.on_event)
    from . import events as _events
    from .voice import VoiceService

    lyst.voice = VoiceService(lyst.stores, lyst.guard, runtime=lyst.runtime, llm=lyst.llm, governance=lyst.governance)
    lyst.runtime.periodic.append(lyst.voice.prune_all)  # transcript and audio retention, idle sessions closed
    _events.CATALOG.update({"voice.settings": ("voice.settings_changed", "workspace")})
