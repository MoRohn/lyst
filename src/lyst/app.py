"""Composition root: builds every service once, for the API process or a test."""

from __future__ import annotations

from dataclasses import dataclass

from .analyst.engine import REGISTRY, Services
from .config import Settings, load_settings
from .data import DataService
from .llm import LLMClient
from .memory import MemoryService
from .runtime import TaskRuntime
from .security import Guard
from .semantic import SemanticService
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

    @classmethod
    def build(cls, settings: Settings | None = None, **runtime_kw) -> Lyst:
        settings = settings or load_settings()
        stores = Stores(settings)
        guard = Guard(stores)
        data = DataService(stores, guard)
        semantic = SemanticService(stores, guard)
        memory = MemoryService(stores, guard)
        llm = LLMClient(settings.llm, stores)
        services = Services(guard, data, semantic, memory, llm, REGISTRY)
        runtime = TaskRuntime(stores, services, **runtime_kw)
        return cls(settings, stores, guard, data, semantic, memory, llm, services, runtime)

    def close(self) -> None:
        self.runtime.stop()
        self.stores.close()
