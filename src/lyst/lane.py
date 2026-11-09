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
    lyst.runtime.periodic.append(lyst.events.dispatch)  # durable consumers: new and due-for-retry events
    lyst.data.events = lyst.events  # dataset.schema_changed, staged with the version it describes
    lyst.monitors.events = lyst.events  # metric.deviated, staged with each monitor finding
    if getattr(lyst, "anticipation", None) is not None:
        lyst.anticipation.events = lyst.events  # anticipation.found, for each new anticipation
    if getattr(lyst, "assets", None) is not None:
        lyst.assets.events = lyst.events  # asset.health_changed, staged with the health update

    from .experiments import ExperimentService

    lyst.experiments = ExperimentService(lyst.stores, lyst.guard, lyst.data, lyst.semantic, lambda: lyst)

    from .workbench import WorkbenchService

    lyst.workbench = WorkbenchService(lyst.stores, lyst.guard, lyst.data, lyst.knowledge, lyst.settings.home)

    from .portability import PortabilityService

    lyst.portability = PortabilityService(lyst.stores, lyst.guard, lyst.credentials, lyst.settings.home)
    lyst.portability.governance = getattr(lyst, 'governance', None)

    from .publishing import PublishingService

    lyst.publishing = PublishingService(lyst.stores, lyst.guard, lyst.data, lyst.runtime)
    lyst.publishing.governance = getattr(lyst, 'governance', None)
    # -- other lanes
    from .specialists.core import UniversalAnalystCore  # noqa: PLC0415 - specializations lane

    lyst.analyst_core = UniversalAnalystCore(lyst)
    # every task, from any page or activity, gets assembled expertise (same router and permission rules as handle)
    lyst.runtime.before_submit.append(lyst.analyst_core.enrich)
    lyst.runtime.after_submit.append(lyst.analyst_core.record)
    if getattr(lyst, "anticipation", None) is not None:  # anticipation looks again soon after relevant events
        lyst.events.subscribers.append(lyst.anticipation.on_event)
    from . import events as _events
    from .voice import VoiceService

    lyst.voice = VoiceService(lyst.stores, lyst.guard, runtime=lyst.runtime, llm=lyst.llm, governance=lyst.governance,
                              core=lambda: getattr(lyst, "analyst_core", None))
    lyst.runtime.periodic.append(lyst.voice.prune_all)  # transcript and audio retention, idle sessions closed
    _events.CATALOG.update({"voice.settings": ("voice.settings_changed", "workspace")})
    from .pins import PinService  # noqa: PLC0415 - UI lane

    lyst.pins = PinService(lyst.stores, lyst.guard)
    from .graphs import GraphService  # noqa: PLC0415 - graph lane

    lyst.graphs = GraphService(lyst.stores, lyst.guard, lyst.runtime, knowledge=lyst.knowledge, semantic=lyst.semantic,
                               data=lyst.data, memory=lyst.memory)
    lyst.runtime.periodic.append(lyst.graphs.tick)  # Builder → Checker → Router, every running graph
    from .harness import HarnessService  # noqa: PLC0415 - harness lane

    lyst.harness = HarnessService(lyst.stores, lyst.guard, lyst.runtime, memory=lyst.memory)
    lyst.runtime.harness = lyst.harness  # applies a harness.update approval when a person decides it
    from .autonomy import AutonomyService  # noqa: PLC0415 - events lane

    lyst.autonomy = AutonomyService(lyst)
    lyst.autonomy.attach()  # durable consumers: rule evaluation, and verify/deliver when an activity's task ends
    lyst.runtime.autonomy = lyst.autonomy
    _events.EMITTED.update({"activity.opened": "a rule matched an event and opened an activity",
                            "activity.changed": "an activity moved to another lifecycle state"})
    from .feedback import FeedbackService  # noqa: PLC0415 - learning lane

    lyst.feedback = FeedbackService(lyst.stores, lyst.guard, lyst.memory)
    from .learning import LearningService  # noqa: PLC0415 - learning lane

    lyst.learning = LearningService(lyst.stores, lyst.guard, lyst.feedback)
    from .quality import QualityService  # noqa: PLC0415 - data quality lane

    lyst.quality = QualityService(lyst.stores, lyst.guard, lyst.data, lyst.runtime)
    lyst.runtime.quality = lyst.quality  # applies a quality.resolve approval when an admin decides it
    from .needs import NeedsService  # noqa: PLC0415 - needs-you lane

    lyst.needs = NeedsService(lyst.stores, lyst.guard, lyst.runtime, lyst.semantic, lyst.data,
                              collab=getattr(lyst, "collab", None))
    from .apps import AppService  # noqa: PLC0415 - AI SDK lane

    lyst.apps = AppService(lyst.stores, lyst.guard, lyst.data, lyst.semantic, lyst.llm)
    if getattr(lyst, "assets", None) is not None:
        lyst.apps.assets = lyst.assets
        lyst.assets.apps = lyst.apps
