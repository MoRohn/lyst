# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What the gateway knows before a model call starts: the workload (routing objective and default
pool), the admission limits, and the tier's model list. Ported from Acumane's
``packages/agent-core/src/ai-gateway/policy.ts`` and ``packages/config/src/tier-rates.ts``; the
values are the same unless noted. See docs/MODELS.md.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Literal

from . import catalog

Pool = Literal["interactive", "background"]
POOLS: tuple[Pool, ...] = ("interactive", "background")

WORKLOADS = ("chat", "long_form", "structured", "extract", "classify", "reasoning", "agent", "embed", "enrich")

DEFAULT_POOL: dict[str, Pool] = {
    "chat": "interactive", "long_form": "interactive", "structured": "interactive", "reasoning": "interactive",
    "agent": "interactive", "extract": "background", "classify": "background", "embed": "background",
    "enrich": "background",
}

# OpenRouter performance thresholds keep its load balancing (an explicit sort would put every call on
# one provider); endpoints that miss them move down the list, not off it. Latency in seconds,
# throughput in tokens a second.
WORKLOAD_ROUTING: dict[str, dict] = {
    "chat": {"provider": {"preferred_max_latency": {"p50": 2, "p90": 5}}, "max_tokens": 2048},
    "agent": {"provider": {"preferred_max_latency": {"p50": 2, "p90": 5}}, "max_tokens": 2048},
    "long_form": {"provider": {"preferred_min_throughput": {"p50": 40, "p90": 20}}, "max_tokens": 8000},
    "structured": {"provider": {"preferred_min_throughput": {"p50": 40, "p90": 20}}, "max_tokens": 4000},
    "reasoning": {"max_tokens": 8000},
    "extract": {"max_tokens": 2000},
    "classify": {"max_tokens": 800},
    "enrich": {"max_tokens": 1500},
    "embed": {"max_tokens": 0},
}

# what a call made while a person waits prefers when its workload sets no routing of its own
INTERACTIVE_LATENCY = {"preferred_max_latency": {"p50": 2, "p90": 5}}

# Short, structured steps (mapping a question to a metric, extracting fields, a layout as JSON, rewording computed
# findings in a few hundred tokens) gain nothing from a model's default thinking and wait seconds for it, or spend
# the whole output budget on it and have to be retried. They run with reasoning off, or at the lowest effort a
# model that always reasons accepts. Long answers and reasoning keep the model's default.
QUICK_WORKLOADS = ("classify", "extract", "structured")
QUICK_MAX_TOKENS = 1024


def is_quick(workload: str, max_tokens: int | None = None) -> bool:
    return workload in QUICK_WORKLOADS or (workload in ("chat", "agent") and max_tokens is not None
                                          and max_tokens <= QUICK_MAX_TOKENS)


def quick_order(models: list[str], refused: set[str] | frozenset = frozenset()) -> list[str]:
    """For a quick call walking a tier: models whose thinking can be lowered (or that do not think by default)
    first, in tier order; models that always think at a high effort, or whose provider refused a lower setting,
    after them."""
    def slow(model: str) -> bool:
        if model in refused:
            return True
        info = (catalog.find(model) or {}).get("reasoningInfo")
        return bool(info and (info.get("mandatory") or info.get("defaultOn")) and quick_reasoning(model, "classify") is None)
    return sorted(models, key=slow)


def quick_reasoning(model: str, workload: str) -> dict | None:
    """The OpenRouter ``reasoning`` setting for a quick call on this model, from the catalog's reasoning info.
    ``workload`` is the call's workload, or "quick" for a short chat call (``is_quick``)."""
    if workload not in (*QUICK_WORKLOADS, "quick"):
        return None
    m = catalog.find(model)
    info = (m or {}).get("reasoningInfo")
    if not info:
        return None  # no reasoning, or a model the catalog does not describe: send nothing
    efforts = info.get("efforts") or []
    lowest = next((e for e in ("minimal", "low") if e in efforts), None)
    if info.get("mandatory"):
        return {"effort": lowest} if lowest else None
    if not info.get("defaultOn"):
        return None
    if "none" in efforts:
        return {"effort": "none"}
    return {"effort": lowest} if lowest else {"enabled": False}


@dataclass(frozen=True)
class PoolLimits:
    max_inflight: int
    per_tenant: int
    max_queue: int
    max_wait_s: float


@dataclass(frozen=True)
class AdmissionLimits:
    max_inflight: int = 48
    interactive_reserve: int = 16
    max_tokens: int = 1_000_000
    per_tenant_tokens: int = 250_000
    pools: dict[str, PoolLimits] = field(default_factory=dict)


def _pos(name: str, fallback: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else fallback
    except ValueError:
        return fallback


def admission_limits() -> AdmissionLimits:
    total = _pos("LYST_AI_MAX_INFLIGHT", 48)
    return AdmissionLimits(
        max_inflight=total,
        interactive_reserve=min(total, _pos("LYST_AI_INTERACTIVE_RESERVE", math.ceil(total / 3))),
        max_tokens=_pos("LYST_AI_MAX_TOKENS", 1_000_000),
        per_tenant_tokens=_pos("LYST_AI_TENANT_TOKENS", 250_000),
        pools={
            "interactive": PoolLimits(_pos("LYST_AI_INTERACTIVE_MAX", math.ceil(total * 2 / 3)),
                                      _pos("LYST_AI_INTERACTIVE_TENANT", 8), 64,
                                      _pos("LYST_AI_INTERACTIVE_WAIT_MS", 8000) / 1000),
            "background": PoolLimits(_pos("LYST_AI_BACKGROUND_MAX", math.ceil(total / 4)),
                                     _pos("LYST_AI_BACKGROUND_TENANT", 3), 32,
                                     _pos("LYST_AI_BACKGROUND_WAIT_MS", 45000) / 1000),
        },
    )


TIERS = ("economy", "balanced", "frontier")


def tier_of(value: str | None) -> str:
    """Legacy names stay valid as in Acumane: standard follows balanced, max follows frontier."""
    if value in ("frontier", "max"):
        return "frontier"
    if value == "economy":
        return "economy"
    return "balanced"


def tier_models(tier: str | None) -> list[dict]:
    """The tier's models from the active catalog (OpenRouter's live list once refreshed), cheapest typical call first."""
    return list(catalog.active()["tiers"][tier_of(tier)])


def attempts(tier: str | None, pinned: str | None = None) -> list[str]:
    """The models a call walks, in order. A pin is used alone, like Acumane's pinned tier model."""
    if pinned:
        return [pinned]
    return [m["modelId"] for m in tier_models(tier)]


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Cost from the catalog's per-token prices when OpenRouter did not report ``usage.cost``."""
    m = catalog.find(model)
    return catalog.call_usd(m, input_tokens, output_tokens) if m else None


def ceiling_usd(ceiling: str | None) -> float:
    """The dearest typical call a workspace may choose under the installation's highest allowed tier."""
    return max(catalog.typical_call_usd(m) for m in tier_models(ceiling or "frontier"))


@dataclass(frozen=True)
class Selection:
    """What a workspace's calls route to. ``mode``: auto walks the tier; pinned uses one model alone."""

    tier: str
    model: str | None
    source: str            # workspace | installation | default
    ceiling: str
    attempts: tuple[str, ...]

    @property
    def mode(self) -> str:
        return "pinned" if self.model else "auto"

    @property
    def route_tier(self) -> str:
        """The tier recorded on a call: the pinned model's own tier when it has one, else 'custom'."""
        if not self.model:
            return self.tier
        return catalog.tier_of_model(self.model) or "custom"


def choice_allowed(tier: str | None, model: str | None, ceiling: str | None) -> str | None:
    """None when a workspace may choose this; otherwise the reason it may not."""
    ceiling = tier_of(ceiling or "frontier")
    if tier and TIERS.index(tier_of(tier)) > TIERS.index(ceiling):
        return f"this installation allows tiers up to {ceiling}"
    if model:
        m = catalog.find(model)
        if m is None:
            return f"{model} is not in OpenRouter's catalog (or cannot call tools); an installation administrator can pin it"
        if catalog.typical_call_usd(m) > ceiling_usd(ceiling) * 1.0001:
            return f"{m['modelLabel']} costs more per call than the {ceiling} tier this installation allows"
    return None


def resolve(*, workspace: tuple[str | None, str | None] = (None, None), installation: tuple[str | None, str | None] = (None, None),
            default_tier: str | None = None, ceiling: str | None = None) -> Selection:
    """Workspace choice, else the installation's, else the environment default. A workspace choice above the
    ceiling (the ceiling was lowered after it was made) falls back to the installation's choice."""
    ceiling = tier_of(ceiling or "frontier")
    ws_tier, ws_model = workspace
    if (ws_tier or ws_model) and choice_allowed(ws_tier, ws_model, ceiling) is None:
        tier, model, source = tier_of(ws_tier or installation[0] or default_tier), ws_model or None, "workspace"
    elif installation[0] or installation[1]:
        tier, model, source = tier_of(installation[0] or default_tier), installation[1] or None, "installation"
    else:
        tier, model, source = tier_of(default_tier), None, "default"
    return Selection(tier, model, source, ceiling, tuple(attempts(tier, model)))
