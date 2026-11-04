"""Configuration. Everything comes from environment variables with safe local defaults.

LYST_HOME            where databases, keys and artifacts live (default ./var)
LYST_MASTER_KEY      hex master key for encryption at rest; if unset a keyfile is created
                     at $LYST_HOME/.lyst-key with mode 0600 (development only)
LYST_ENCRYPT         "1" (default) encrypts every DuckDB file; "0" disables (tests only)
LYST_OPENROUTER_API_KEY  an OpenRouter key; when set, OpenRouter is the provider (Acumane's rails)
LYST_LLM_TIER        economy | balanced (default) | frontier: the model list a call walks
LYST_LLM_MODEL       pin one model (an OpenRouter slug, or the local model's name)
LYST_LLM_PROVIDER    openrouter | local; inferred when unset
LYST_LLM_BASE_URL    a local OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 (Ollama)
LYST_LLM_API_KEY     optional bearer key for the local endpoint
LYST_LLM_EXTERNAL    "1" marks a non-OpenRouter endpoint as external (data policy applies)
LYST_EXTERNAL_LLM_DEFAULT  "1" lets new workspaces use an external provider (default: on with OpenRouter)
LYST_API_URL         where the Streamlit UI finds the API (default http://127.0.0.1:8770)
"""

from __future__ import annotations

import os
import secrets
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
    external_default: bool = False

    @property
    def configured(self) -> bool:
        if self.provider == "openrouter":
            return bool(self.api_key)
        return bool(self.base_url and self.model)


def llm_settings_from_env() -> LLMSettings:
    env = os.environ
    key = env.get("LYST_OPENROUTER_API_KEY") or None
    provider = env.get("LYST_LLM_PROVIDER") or ("openrouter" if key else ("local" if env.get("LYST_LLM_BASE_URL") else None))
    if provider == "openrouter":
        return LLMSettings(base_url=env.get("LYST_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
                           model=env.get("LYST_LLM_MODEL") or None, api_key=key, external=True,
                           provider="openrouter", tier=env.get("LYST_LLM_TIER", "balanced"),
                           external_default=env.get("LYST_EXTERNAL_LLM_DEFAULT", "1") == "1")
    return LLMSettings(base_url=env.get("LYST_LLM_BASE_URL") or None, model=env.get("LYST_LLM_MODEL") or None,
                       api_key=env.get("LYST_LLM_API_KEY") or None, external=env.get("LYST_LLM_EXTERNAL", "0") == "1",
                       provider=provider, tier=env.get("LYST_LLM_TIER", "balanced"),
                       external_default=env.get("LYST_EXTERNAL_LLM_DEFAULT", "0") == "1")


@dataclass(frozen=True)
class Settings:
    home: Path
    master_key: bytes
    encrypt: bool = True
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


def _master_key(home: Path) -> bytes:
    env = os.environ.get("LYST_MASTER_KEY")
    if env:
        key = bytes.fromhex(env)
        if len(key) < 32:
            raise ValueError("LYST_MASTER_KEY must be at least 32 bytes of hex")
        return key
    path = home / ".lyst-key"
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    home.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return key


def load_settings(home: str | Path | None = None) -> Settings:
    root = Path(home or os.environ.get("LYST_HOME", "var")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return Settings(
        home=root,
        master_key=_master_key(root),
        encrypt=os.environ.get("LYST_ENCRYPT", "1") != "0",
        api_url=os.environ.get("LYST_API_URL", "http://127.0.0.1:8770"),
        llm=llm_settings_from_env(),
        workers=int(os.environ.get("LYST_WORKERS", "2")),
    )
