# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Configuration. Everything comes from environment variables with safe local defaults.

LYST_HOME            where databases, keys and artifacts live (default ./var)
LYST_MASTER_KEY      hex master key for encryption at rest. Comes from a trust anchor outside Lyst's data: a mounted
                     secret file, OpenBao, the OS credential store (setup mode or `lyst init` puts it there) or the
                     environment (secrets/masterkey.py). With none, the engine starts in setup mode; it never
                     generates a key beside the database (LYST_DEV_INSECURE_KEYFILE=1 allows a dev keyfile).
LYST_ENCRYPT         "1" (default) encrypts every DuckDB file; "0" disables (tests only)
LYST_OPENROUTER_API_KEY  optional seed for unattended deployments (mounted file or environment): imported once as the
                     installation's OpenRouter credential. Normally the key is entered in Settings › Secrets.
LYST_OPENBAO_URL     enterprise: keep secrets (and optionally the master key) in OpenBao KV v2; see docs/SECURITY.md
LYST_LLM_TIER        economy | balanced (default) | frontier: the default level (workspaces may choose their own)
LYST_LLM_MAX_TIER    the highest level a workspace may choose (default frontier)
LYST_CATALOG_AUTO    "0" turns off the daily refresh of OpenRouter's model list and prices
LYST_LLM_MODEL       pin one model (an OpenRouter slug, or the local model's name)
LYST_LLM_PROVIDER    openrouter | local; inferred when unset
LYST_LLM_BASE_URL    a local OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 (Ollama)
LYST_LLM_API_KEY     optional bearer key for the local endpoint
LYST_LLM_EXTERNAL    "1" marks a non-OpenRouter endpoint as external (data policy applies)
LYST_EMBED_MODEL     embedding model at the same provider (e.g. openai/text-embedding-3-small on OpenRouter,
                     nomic-embed-text on Ollama); turns on semantic search over documents and memory
LYST_EXTERNAL_LLM_DEFAULT  "1"/"0": whether new workspaces may use an external provider (default: on once the
                     installation has connected OpenRouter in Settings › Secrets & Connections)
LYST_API_URL         where the Streamlit UI finds the API (default http://127.0.0.1:8770)
LYST_PROFILE         local (default) | offline | server | portable: a set of defaults for a kind of installation
                     (lyst/profiles.py); `lyst doctor` checks the installation against it
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LLMSettings:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    external: bool = False
    provider: str | None = None  # "openrouter" | "local" | None
    tier: str = "balanced"
    external_default: bool | None = False   # None: on once the installation has connected OpenRouter

    @property
    def configured(self) -> bool:
        """Static configuration only; whether a key exists is answered at call time by the secret store."""
        if self.provider == "openrouter":
            return True
        return bool(self.base_url and self.model)


def llm_settings_from_env() -> LLMSettings:
    env = os.environ
    provider = env.get("LYST_LLM_PROVIDER") or ("local" if env.get("LYST_LLM_BASE_URL") else "openrouter")
    if provider == "openrouter":
        return LLMSettings(base_url=env.get("LYST_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                           model=env.get("LYST_LLM_MODEL") or None, api_key=None, external=True,
                           provider="openrouter", tier=env.get("LYST_LLM_TIER", "balanced"),
                           external_default=(env["LYST_EXTERNAL_LLM_DEFAULT"] == "1") if env.get("LYST_EXTERNAL_LLM_DEFAULT") else None)
    return LLMSettings(base_url=env.get("LYST_LLM_BASE_URL") or None, model=env.get("LYST_LLM_MODEL") or None,
                       api_key=anchor("LYST_LLM_API_KEY"), external=env.get("LYST_LLM_EXTERNAL", "0") == "1",
                       provider=provider, tier=env.get("LYST_LLM_TIER", "balanced"),
                       external_default=env.get("LYST_EXTERNAL_LLM_DEFAULT", "0") == "1")


@dataclass(frozen=True)
class Settings:
    home: Path
    master_key: bytes | None          # None: no trust anchor holds it yet (setup mode)
    encrypt: bool = True
    master_key_source: str | None = None
    api_url: str = "http://127.0.0.1:8770"
    llm: LLMSettings = field(default_factory=LLMSettings)
    workers: int = 2

    @property
    def control_db(self) -> Path:
        return self.home / "control.duckdb"

    def workspace_db(self, workspace_id: str) -> Path:
        return self.home / "workspaces" / f"{workspace_id}.duckdb"

    def artifact_dir(self, workspace_id: str) -> Path:
        return self.home / "artifacts" / workspace_id

    @property
    def setup_required(self) -> bool:
        return self.encrypt and self.master_key is None


def _master_key(home: Path) -> tuple[bytes | None, str | None]:
    from .secrets import masterkey

    mk = masterkey.resolve(home)
    return (mk.key, mk.source) if mk else (None, None)


def load_env_file() -> Path | None:
    """Read non-secret settings from LYST_ENV_FILE or ./.env without overriding variables already set.
    Secret-shaped names (keys, passwords, tokens) in that file are refused with a warning: secrets come
    from the OS credential store, a mounted secret file or the orchestrator (see security/vault.py)."""
    import logging

    from dotenv import dotenv_values

    from .security.vault import is_secret_name

    for candidate in (os.environ.get("LYST_ENV_FILE"), ".env"):
        if candidate and Path(candidate).is_file():
            for k, v in dotenv_values(candidate).items():
                if v is None:
                    continue
                if is_secret_name(k):
                    logging.getLogger("lyst.config").warning(
                        "%s in %s ignored: credentials are entered in Settings › Secrets & Connections (or mounted under "
                        "$LYST_SECRETS_DIR for unattended deployments), never read from .env", k, candidate)
                    continue
                os.environ.setdefault(k, v)
            return Path(candidate).resolve()
    return None


def anchor(name: str) -> str | None:
    """A bootstrap value from a trust anchor (mounted file, OS credential store, environment). Never cached."""
    from .security.vault import anchor as _anchor

    return _anchor(name)


def load_settings(home: str | Path | None = None) -> Settings:
    load_env_file()
    from .profiles import apply as apply_profile  # noqa: PLC0415 - LYST_PROFILE fills defaults before anything reads them

    apply_profile()
    root = Path(home or os.environ.get("LYST_HOME", "var")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key, source = _master_key(root)
    return Settings(
        home=root,
        master_key=key,
        master_key_source=source,
        encrypt=os.environ.get("LYST_ENCRYPT", "1") != "0",
        api_url=os.environ.get("LYST_API_URL", "http://127.0.0.1:8770"),
        llm=llm_settings_from_env(),
        workers=int(os.environ.get("LYST_WORKERS", "2")),
    )
