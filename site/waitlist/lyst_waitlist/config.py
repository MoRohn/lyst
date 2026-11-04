"""Configuration from environment variables. Secrets are only ever read from the environment."""

from __future__ import annotations

import base64
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("lyst_waitlist")


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _rate(value: str) -> tuple[int, int]:
    try:
        count, window = value.split("/", 1)
        parsed = int(count), int(window)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"LYST_WAITLIST_RATE_LIMIT must look like '5/600', got {value!r}") from exc
    if parsed[0] < 1 or parsed[1] < 1:
        raise ConfigError("LYST_WAITLIST_RATE_LIMIT values must be positive")
    return parsed


@dataclass(frozen=True)
class Settings:
    db_path: Path
    allowed_origins: tuple[str, ...]
    hash_key: bytes
    encryption_key: bytes | None
    rate_limit: tuple[int, int] = (5, 600)
    retention_days: int = 730
    trust_proxy: bool = False
    max_body_bytes: int = 8192
    environment: str = "production"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        environment = env.get("LYST_WAITLIST_ENV", "production").strip().lower()
        dev = environment == "development"

        origins = tuple(o.strip().rstrip("/") for o in env.get("LYST_WAITLIST_ALLOWED_ORIGINS", "").split(",") if o.strip())
        if not origins:
            if not dev:
                raise ConfigError("LYST_WAITLIST_ALLOWED_ORIGINS is required (comma-separated site origins).")
            origins = ("http://localhost:4321", "http://127.0.0.1:4321")

        hash_key_raw = env.get("LYST_WAITLIST_HASH_KEY", "").strip()
        if hash_key_raw:
            hash_key = hash_key_raw.encode()
        elif dev:
            log.warning("LYST_WAITLIST_HASH_KEY not set; using an ephemeral development key")
            hash_key = secrets.token_bytes(32)
        else:
            raise ConfigError("LYST_WAITLIST_HASH_KEY is required in production.")
        if len(hash_key) < 32:
            raise ConfigError("LYST_WAITLIST_HASH_KEY must be at least 32 characters.")

        enc_raw = env.get("LYST_WAITLIST_ENCRYPTION_KEY", "").strip()
        encryption_key: bytes | None
        if enc_raw:
            try:
                if len(base64.urlsafe_b64decode(enc_raw)) != 32:
                    raise ValueError
            except ValueError as exc:
                raise ConfigError("LYST_WAITLIST_ENCRYPTION_KEY must be a Fernet key (lyst-waitlist genkey).") from exc
            encryption_key = enc_raw.encode()
        elif _bool(env.get("LYST_WAITLIST_ALLOW_PLAINTEXT")) or dev:
            log.warning("Field encryption disabled: personal data is stored in plaintext")
            encryption_key = None
        else:
            raise ConfigError(
                "LYST_WAITLIST_ENCRYPTION_KEY is required in production "
                "(or set LYST_WAITLIST_ALLOW_PLAINTEXT=1 if the volume is encrypted and you accept that)."
            )

        return cls(
            db_path=Path(env.get("LYST_WAITLIST_DB", "data/waitlist.duckdb")),
            allowed_origins=origins,
            hash_key=hash_key,
            encryption_key=encryption_key,
            rate_limit=_rate(env.get("LYST_WAITLIST_RATE_LIMIT", "5/600")),
            retention_days=int(env.get("LYST_WAITLIST_RETENTION_DAYS", "730")),
            trust_proxy=_bool(env.get("LYST_WAITLIST_TRUST_PROXY")),
            environment=environment,
        )
