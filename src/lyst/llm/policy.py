# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What the gateway knows before a model call starts: the workload (routing objective and default
pool), the admission limits, and the tier's model list. Ported from Acumane's
``packages/agent-core/src/ai-gateway/policy.ts`` and ``packages/config/src/tier-rates.ts``; the
values are the same unless noted. See docs/MODELS.md.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

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


@cache
def _catalog() -> dict:
    return json.loads((Path(__file__).parent / "tiers.json").read_text())


def tier_of(value: str | None) -> str:
    """Legacy names stay valid as in Acumane: standard follows balanced, max follows frontier."""
    if value in ("frontier", "max"):
        return "frontier"
    if value == "economy":
        return "economy"
    return "balanced"


def tier_models(tier: str | None) -> list[dict]:
    return list(_catalog()["tiers"][tier_of(tier)])


def attempts(tier: str | None, pinned: str | None = None) -> list[str]:
    """The models a call walks, in order. A pin is used alone, like Acumane's pinned tier model."""
    if pinned:
        return [pinned]
    return [m["modelId"] for m in tier_models(tier)]


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    for tier in _catalog()["tiers"].values():
        for m in tier:
            if m["modelId"] == model:
                return (input_tokens or 0) * m["inputUsdPerToken"] + (output_tokens or 0) * m["outputUsdPerToken"]
    return None
