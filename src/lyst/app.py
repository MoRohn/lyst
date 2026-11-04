# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Composition root: builds every service once, for the API process or a test."""

from __future__ import annotations

from dataclasses import dataclass

from .analyst.engine import REGISTRY, Services
from .config import Settings, load_settings
from .data import DataService
from .deliverables import ArtifactService
from .knowledge import KnowledgeService
from .llm import LLMClient
from .memory import MemoryService
from .monitoring import MonitorService
from .runtime import TaskRuntime
from .security import Guard
from .semantic import SemanticService
from .skills import SkillService
from .store import Stores


@dataclass
class Lyst:
    settings: Settings
    stores: Stores
    guard: Guard
    data: DataService
    semantic: SemanticService
    memory: MemoryService
    llm: LLMClient
    services: Services
    runtime: TaskRuntime
    skills: SkillService
    artifacts: ArtifactService
    knowledge: KnowledgeService
    monitors: MonitorService

    @classmethod
    def build(cls, settings: Settings | None = None, **runtime_kw) -> Lyst:
        settings = settings or load_settings()
        stores = Stores(settings)
        guard = Guard(stores)
        data = DataService(stores, guard)
        knowledge = KnowledgeService(stores, guard)
        semantic = SemanticService(stores, guard, knowledge)
        memory = MemoryService(stores, guard)
        llm = LLMClient(settings.llm, stores)
        skills = SkillService(stores, guard, data)
        services = Services(guard, data, semantic, memory, llm, REGISTRY, skills, knowledge)
        runtime = TaskRuntime(stores, services, **runtime_kw)
        artifacts = ArtifactService(stores, guard, settings.home, knowledge)
        monitors = MonitorService(stores, guard, data, semantic, runtime)
        runtime.monitors = monitors
        services.monitors = monitors
        return cls(settings, stores, guard, data, semantic, memory, llm, services, runtime, skills, artifacts, knowledge, monitors)

    def close(self) -> None:
        self.runtime.stop()
        self.stores.close()
