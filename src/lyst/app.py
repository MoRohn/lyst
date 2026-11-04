# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Composition root: builds every service once, for the API process or a test."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import observability as obs
from .analyst.engine import REGISTRY, Services
from .assets import AssetService
from .catalog import CatalogService
from .collab import CollaborationService
from .config import Settings, load_settings
from .connectors import ConnectionService
from .contracts import ContractService
from .data import DataService
from .deliverables import ArtifactService
from .ingest import IngestService
from .knowledge import KnowledgeService
from .llm import LLMClient
from .memory import MemoryService
from .monitoring import MonitorService
from .onboarding import OnboardingService
from .runtime import TaskRuntime
from .secrets import CredentialService, backend_from_env
from .security import Guard
from .semantic import SemanticService
from .skills import SkillService
from .store import Stores
from .studio import StudioService
from .transforms import TransformService
from .workflows import WorkflowService


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
    studio: StudioService
    connections: ConnectionService
    credentials: CredentialService
    catalog: CatalogService
    contracts: ContractService
    assets: AssetService
    collab: CollaborationService
    onboarding: OnboardingService | None = None
    transforms: TransformService | None = None
    ingest: IngestService | None = None

    @classmethod
    def build(cls, settings: Settings | None = None, **runtime_kw) -> Lyst:
        settings = settings or load_settings()
        stores = Stores(settings)
        guard = Guard(stores)
        credentials = CredentialService(stores, guard, backend_from_env(stores, settings.master_key or b""))
        credentials.redactor.install()
        from .security.vault import anchor  # noqa: PLC0415

        credentials.seed_from_anchors(anchor)
        chain = credentials.verify_audit_chain()
        obs.AUDIT_CHAIN.set(1 if chain["ok"] else 0)
        if not chain["ok"]:
            logging.getLogger("lyst.credentials").error("credential audit chain broken at event %s: the history was altered", chain.get("broken_at"))
        data = DataService(stores, guard)
        llm = LLMClient(settings.llm, stores, credentials=credentials)
        if settings.llm.provider == "openrouter":
            from .llm.catalog import ModelCatalog  # noqa: PLC0415

            llm.catalog = ModelCatalog(stores, base_url=settings.llm.base_url or "https://openrouter.ai/api/v1")
        knowledge = KnowledgeService(stores, guard, llm)
        semantic = SemanticService(stores, guard, knowledge)
        memory = MemoryService(stores, guard, llm)
        skills = SkillService(stores, guard, data)
        services = Services(guard, data, semantic, memory, llm, REGISTRY, skills, knowledge)
        services.workflows = WorkflowService(stores, guard, data, semantic, REGISTRY)
        runtime = TaskRuntime(stores, services, **runtime_kw)
        artifacts = ArtifactService(stores, guard, settings.home, knowledge)
        monitors = MonitorService(stores, guard, data, semantic, runtime)
        runtime.monitors = monitors
        services.monitors = monitors
        services.runtime = runtime
        studio = StudioService(stores, guard, data, semantic, runtime, settings.home, knowledge)
        services.studio = studio
        connections = ConnectionService(stores, guard, data, credentials)
        services.connections = connections
        catalog = CatalogService(stores, guard, data, semantic, knowledge)
        contracts = ContractService(stores, guard, catalog, semantic)
        catalog.contracts = contracts
        connections.catalog = catalog
        data.after_ingest += [catalog.after_ingest, contracts.after_ingest]
        services.catalog = catalog
        assets = AssetService(stores, guard, semantic=semantic, runtime=runtime, artifacts=artifacts, studio=studio)
        artifacts.assets = studio.assets = services.workflows.assets = assets
        transforms = TransformService(stores, guard, data, knowledge=knowledge, llm=llm, catalog=catalog)
        transforms.assets, assets.transforms = assets, transforms
        data.after_ingest.append(transforms.after_ingest)  # derived datasets follow their inputs before assets are assessed
        data.after_ingest.append(assets.after_ingest)  # after the contract check, so a breaking version reads as broken
        semantic.after_change.append(assets.after_definition)
        runtime.periodic.append(assets.complete_all)
        collab = CollaborationService(stores, guard, semantic=semantic, contracts=contracts, assets=assets)
        onboarding = OnboardingService(stores, guard, catalog=catalog, semantic=semantic, contracts=contracts, data=data,
                                       knowledge=knowledge, runtime=runtime)
        ingest = IngestService(stores, guard, data, knowledge)
        return cls(settings, stores, guard, data, semantic, memory, llm, services, runtime, skills, artifacts, knowledge, monitors,
                   studio, connections, credentials, catalog, contracts, assets, collab, onboarding=onboarding, transforms=transforms, ingest=ingest)

    def close(self) -> None:
        self.runtime.stop()
        self.stores.close()
