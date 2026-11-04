"""Capability registry. A capability is a typed, permission-scoped, testable unit of work.

The analyst plans with capabilities, the runtime executes them as checkpointed steps, and
the Build workspace registers new ones (as reviewed skills). Nothing calls a capability
that is not registered, and the registry checks the caller's permission before it runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    workspace_id: str
    actor: Any  # security.Principal
    task_id: str | None
    services: Any  # analyst.engine.Services
    outputs: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    agent: str
    phase: str
    permission: str
    fn: Callable[[Context, dict[str, Any]], dict[str, Any]]
    mutating: bool = False
    requires_approval: bool = False
    version: int = 1
    origin: str = "core"


class Registry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> Capability:
        if cap.name in self._caps and self._caps[cap.name].version >= cap.version:
            raise ValueError(f"capability {cap.name} v{self._caps[cap.name].version} is already registered")
        self._caps[cap.name] = cap
        return cap

    def get(self, name: str) -> Capability:
        try:
            return self._caps[name]
        except KeyError:
            raise KeyError(f"no capability named {name}") from None

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": c.name, "description": c.description, "agent": c.agent, "phase": c.phase,
             "permission": c.permission, "mutating": c.mutating, "requires_approval": c.requires_approval,
             "version": c.version, "origin": c.origin}
            for c in sorted(self._caps.values(), key=lambda c: c.name)
        ]

    def run(self, name: str, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
        cap = self.get(name)
        ctx.services.guard.require(ctx.actor, ctx.workspace_id, cap.permission, name)
        return cap.fn(ctx, args)


def capability(registry: Registry, name: str, *, agent: str, phase: str, permission: str,
               description: str, mutating: bool = False, requires_approval: bool = False):
    def wrap(fn):
        registry.register(Capability(name, description, agent, phase, permission, fn, mutating,
                                     requires_approval))
        return fn

    return wrap


AGENTS = {
    "lead_analyst": "Owns the objective, the plan and the final deliverable.",
    "business_analyst": "Interprets the question, KPIs and business terms.",
    "data_discovery": "Finds the datasets, fields and definitions that apply.",
    "analytics_engineer": "Compiles governed metrics into SQL.",
    "statistical_analyst": "Attribution, variance, anomaly and significance work.",
    "data_scientist": "Forecasts and models.",
    "quality": "Independently checks numbers, coverage and reconciliation.",
    "communications": "Writes the answer in Answer, Evidence, Implications, Actions order.",
    "knowledge_curator": "Records what was learned, with provenance.",
}
