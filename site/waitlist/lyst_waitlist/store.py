"""DuckDB storage with a single controlled writer.

DuckDB allows one read-write process per database file. This module owns the
only connection in the process and serializes every statement through a lock,
so concurrent requests (FastAPI runs sync handlers on a thread pool) never
interleave writes. Run the service with exactly one worker process.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from cryptography.fernet import Fernet

SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    id              UUID PRIMARY KEY,
    kind            VARCHAR NOT NULL,
    email_hash      VARCHAR NOT NULL,
    email           VARCHAR NOT NULL,      -- Fernet token when encryption is enabled
    role            VARCHAR,
    organization    VARCHAR,               -- Fernet token when encryption is enabled
    interest        VARCHAR,
    team_size       VARCHAR,
    message         VARCHAR,               -- Fernet token when encryption is enabled
    consent_scope   VARCHAR NOT NULL,
    consent_version VARCHAR NOT NULL,
    consent_at      TIMESTAMP NOT NULL,     -- UTC
    origin          VARCHAR,
    created_at      TIMESTAMP NOT NULL,     -- UTC
    UNIQUE (kind, email_hash)
);
"""

CONSENT_SCOPES = {
    "early_access": "early_access_updates",
    "design_partner": "design_partner_contact",
}


@dataclass(frozen=True)
class Registration:
    kind: str
    email: str
    consent_version: str
    role: str | None = None
    organization: str | None = None
    interest: str | None = None
    team_size: str | None = None
    message: str | None = None
    origin: str | None = None


class Store:
    def __init__(self, path: Path, hash_key: bytes, encryption_key: bytes | None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(path))
        self._con.execute(SCHEMA)
        self._hash_key = hash_key
        self._fernet = Fernet(encryption_key) if encryption_key else None

    # -- helpers -------------------------------------------------------------
    def email_hash(self, email: str) -> str:
        return hmac.new(self._hash_key, email.strip().lower().encode(), hashlib.sha256).hexdigest()

    def _seal(self, value: str | None) -> str | None:
        if value is None or self._fernet is None:
            return value
        return self._fernet.encrypt(value.encode()).decode()

    def _open(self, value: str | None) -> str | None:
        if value is None or self._fernet is None:
            return value
        return self._fernet.decrypt(value.encode()).decode()

    # -- writes --------------------------------------------------------------
    def add(self, reg: Registration) -> bool:
        """Insert a registration. Returns False if it already existed (idempotent)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        h = self.email_hash(reg.email)
        with self._lock:
            exists = self._con.execute(
                "SELECT 1 FROM registrations WHERE kind = ? AND email_hash = ?", [reg.kind, h]
            ).fetchone()
            if exists:
                return False
            self._con.execute(
                """INSERT INTO registrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    uuid.uuid4(),
                    reg.kind,
                    h,
                    self._seal(reg.email.strip().lower()),
                    reg.role,
                    self._seal(reg.organization),
                    reg.interest,
                    reg.team_size,
                    self._seal(reg.message),
                    CONSENT_SCOPES[reg.kind],
                    reg.consent_version,
                    now,
                    reg.origin,
                    now,
                ],
            )
            return True

    def remove(self, email: str) -> int:
        """Delete every registration for an email address. Returns rows removed."""
        with self._lock:
            n = self._con.execute(
                "SELECT count(*) FROM registrations WHERE email_hash = ?", [self.email_hash(email)]
            ).fetchone()[0]
            self._con.execute("DELETE FROM registrations WHERE email_hash = ?", [self.email_hash(email)])
            return int(n)

    def purge_older_than(self, days: int) -> int:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        with self._lock:
            n = self._con.execute("SELECT count(*) FROM registrations WHERE created_at < ?", [cutoff]).fetchone()[0]
            self._con.execute("DELETE FROM registrations WHERE created_at < ?", [cutoff])
            return int(n)

    # -- reads (operator only; never exposed over HTTP) ----------------------
    def count(self, kind: str | None = None) -> int:
        with self._lock:
            if kind:
                return int(self._con.execute("SELECT count(*) FROM registrations WHERE kind = ?", [kind]).fetchone()[0])
            return int(self._con.execute("SELECT count(*) FROM registrations").fetchone()[0])

    def export(self) -> list[dict]:
        with self._lock:
            cur = self._con.execute(
                "SELECT kind, email, role, organization, interest, team_size, message, consent_scope, "
                "consent_version, consent_at, created_at FROM registrations ORDER BY created_at"
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        out = []
        for row in rows:
            rec = dict(zip(cols, row, strict=True))
            for key in ("email", "organization", "message"):
                rec[key] = self._open(rec[key])
            for key in ("consent_at", "created_at"):
                rec[key] = rec[key].isoformat() + "Z"
            out.append(rec)
        return out

    def close(self) -> None:
        with self._lock:
            self._con.close()
