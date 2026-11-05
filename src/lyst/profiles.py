# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Deployment profiles: one name that sets the defaults a kind of installation needs.

``LYST_PROFILE`` picks one. A profile only fills in variables that are not already set, so any single setting can
still be overridden. ``lyst doctor`` (and ``GET /v1/diagnostics``) checks the running installation against its
profile's rules and says what breaks them.

* ``local`` (default): one machine, loopback only, OpenRouter or a local model.
* ``offline``: air-gapped. A local OpenAI-compatible model (``LYST_LLM_BASE_URL``, such as Ollama or vLLM) is the only
  model; nothing calls the internet: no model catalog refresh, no OpenRouter, webhooks only to private addresses,
  telemetry export off unless pointed at a local collector. Connectors to internet services are flagged.
* ``server``: a shared installation behind a reverse proxy: binds all interfaces, expects an identity provider
  (OIDC) and TLS at the proxy, two or more workers.
* ``portable``: a self-contained folder (``LYST_HOME`` beside the program, everything inside it) that moves between
  machines as a workspace pack (``lyst export`` / ``lyst import``); the master key stays in each machine's own
  credential store, never in the folder.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    "local": {"env": {}, "rules": {"loopback": True}},
    "offline": {"env": {"LYST_LLM_PROVIDER": "local", "LYST_CATALOG_AUTO": "0", "LYST_LLM_EXTERNAL": "0",
                        "LYST_EXTERNAL_LLM_DEFAULT": "0", "LYST_WEBHOOK_ALLOW_PRIVATE": "1", "LYST_OFFLINE": "1",
                        "OTEL_SDK_DISABLED": "true"},
                "rules": {"no_internet": True, "local_model": True}},
    "server": {"env": {"LYST_HOST": "0.0.0.0", "LYST_WORKERS": "4"},  # noqa: S104 - a server profile binds all interfaces
               "rules": {"oidc": True, "tls_at_proxy": True}},
    "portable": {"env": {"LYST_CATALOG_AUTO": "0"}, "rules": {"loopback": True, "self_contained": True}},
}


def name(env: Mapping[str, str] | None = None) -> str:
    """The chosen profile; an unknown name is an error everywhere it is read, never a silent fallback."""
    env = os.environ if env is None else env
    chosen = (env.get("LYST_PROFILE") or "local").strip().lower() or "local"
    if chosen not in PROFILES:
        raise ValueError(f"LYST_PROFILE={chosen!r} is not a profile; choose one of {', '.join(PROFILES)}")
    return chosen


def apply(env: dict[str, str] | None = None) -> str:
    """Fill the chosen profile's defaults into the environment (never overriding what is set). Returns its name."""
    env = os.environ if env is None else env
    chosen = name(env)
    for k, v in PROFILES[chosen]["env"].items():
        env.setdefault(k, v)
    return chosen


def offline() -> bool:
    return os.environ.get("LYST_OFFLINE", "0") == "1" or name() == "offline"


def rules() -> dict[str, Any]:
    return PROFILES[name()]["rules"]
