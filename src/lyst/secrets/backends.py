# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Where encrypted secret values live. Two interchangeable backends behind one small interface:

* **Embedded** (local installations): AES-256-GCM envelope encryption in the control database. Each
  record has its own random 256-bit data key; the value is sealed with a fresh 96-bit nonce and
  authenticated metadata (the record's reference and key version); the data key is wrapped with a
  key-encryption key derived from the installation's master key by HKDF. The master key is never in
  the database, a backup or the repository (see ``masterkey.py``). Decryption failures are reported as
  ``SecretUnavailable`` without echoing anything.
* **OpenBao** (enterprise): values live in OpenBao's KV v2 engine under a scoped path; Lyst authenticates
  with an AppRole or a token it reads from a trust anchor, holding only the policy it needs. The master
  key for the DuckDB files can also be served from OpenBao.

Callers never see a backend's internals: they store and fetch by opaque reference.
"""

from __future__ import annotations

import hashlib
import json
import secrets as rnd
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretUnavailable(RuntimeError):
    """The value could not be produced (missing, revoked, wrong key, backend down). Never carries the value."""


@dataclass(frozen=True)
class StoredRef:
    ref: str
    backend: str
    key_version: int


class SecretBackend(ABC):
    name: str

    @abstractmethod
    def store(self, ref: str, value: str) -> StoredRef: ...

    @abstractmethod
    def fetch(self, ref: str) -> str: ...

    @abstractmethod
    def delete(self, ref: str) -> bool: ...

    def health(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True}


# ----------------------------------------------------------------------------------- embedded


def derive_kek(master_key: bytes, version: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"lyst:secrets:kek:v{version}".encode()).derive(master_key)


class EmbeddedBackend(SecretBackend):
    name = "embedded"

    def __init__(self, stores, master_key: bytes, key_version: int | None = None):
        self.stores = stores
        if key_version is None:
            key_version = int(stores.control.scalar("SELECT value FROM installation_settings WHERE key = 'KEK_VERSION'") or 1)
        self._kek = derive_kek(master_key, key_version)
        self.key_version = key_version

    @staticmethod
    def _aad(ref: str, key_version: int) -> bytes:
        return f"{ref}|v{key_version}".encode()

    def store(self, ref: str, value: str) -> StoredRef:
        dek = AESGCM.generate_key(bit_length=256)
        nonce, dek_nonce = rnd.token_bytes(12), rnd.token_bytes(12)
        aad = self._aad(ref, self.key_version)
        ciphertext = AESGCM(dek).encrypt(nonce, value.encode(), aad)
        wrapped = AESGCM(self._kek).encrypt(dek_nonce, dek, aad)
        self.stores.control.execute(
            "INSERT OR REPLACE INTO secret_blobs (ref, key_version, nonce, ciphertext, dek_nonce, wrapped_dek, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, now())", [ref, self.key_version, nonce, ciphertext, dek_nonce, wrapped])
        return StoredRef(ref, self.name, self.key_version)

    def fetch(self, ref: str) -> str:
        rows = self.stores.control.query("SELECT * FROM secret_blobs WHERE ref = ?", [ref])
        if not rows:
            raise SecretUnavailable("no such secret")
        r = rows[0]
        if int(r["key_version"]) != self.key_version:
            raise SecretUnavailable("secret sealed under another key version; rotate the master key to re-wrap it")
        aad = self._aad(ref, int(r["key_version"]))
        try:
            dek = AESGCM(self._kek).decrypt(bytes(r["dek_nonce"]), bytes(r["wrapped_dek"]), aad)
            return AESGCM(dek).decrypt(bytes(r["nonce"]), bytes(r["ciphertext"]), aad).decode()
        except InvalidTag as exc:
            raise SecretUnavailable("the secret cannot be opened with this installation's master key") from exc

    def delete(self, ref: str) -> bool:
        n = self.stores.control.scalar("SELECT count(*) FROM secret_blobs WHERE ref = ?", [ref])
        self.stores.control.execute("DELETE FROM secret_blobs WHERE ref = ?", [ref])
        return bool(n)

    def rewrap_all(self, new_master_key: bytes, new_version: int) -> int:
        """Root-key rotation for the blobs: every data key is unwrapped with the old KEK and wrapped with the
        new one, in one transaction, so a failure leaves everything under the old key."""
        new_kek = derive_kek(new_master_key, new_version)
        rows = self.stores.control.query("SELECT ref, key_version, nonce, ciphertext, dek_nonce, wrapped_dek FROM secret_blobs")
        updates = []
        for r in rows:
            if int(r["key_version"]) != self.key_version:
                raise SecretUnavailable(f"{r['ref']} is under key version {r['key_version']}, not {self.key_version}")
            old_aad = self._aad(r["ref"], self.key_version)
            new_aad = self._aad(r["ref"], new_version)
            dek = AESGCM(self._kek).decrypt(bytes(r["dek_nonce"]), bytes(r["wrapped_dek"]), old_aad)
            plain = AESGCM(dek).decrypt(bytes(r["nonce"]), bytes(r["ciphertext"]), old_aad)
            nonce, dek_nonce = rnd.token_bytes(12), rnd.token_bytes(12)
            updates.append((r["ref"], new_version, nonce, AESGCM(dek).encrypt(nonce, plain, new_aad), dek_nonce,
                            AESGCM(new_kek).encrypt(dek_nonce, dek, new_aad)))

        def tx(c):
            for ref, kv, nonce, ct, dn, wd in updates:
                c.execute("UPDATE secret_blobs SET key_version = ?, nonce = ?, ciphertext = ?, dek_nonce = ?, wrapped_dek = ? WHERE ref = ?",
                          [kv, nonce, ct, dn, wd, ref])
            c.execute("INSERT OR REPLACE INTO installation_settings (key, value, set_by, updated_at) VALUES ('KEK_VERSION', ?, 'system', now())",
                      [str(new_version)])

        self.stores.control.write(tx)
        self._kek, self.key_version = new_kek, new_version
        return len(updates)


# ------------------------------------------------------------------------------------ OpenBao


class OpenBaoBackend(SecretBackend):
    """KV v2 at ``<url>/v1/<mount>/data/<prefix>/<ref>``. Authentication: a token from a trust anchor, or an
    AppRole (role_id + secret_id) exchanged for a token and renewed before it expires."""

    name = "openbao"

    def __init__(self, url: str, *, mount: str = "secret", prefix: str = "lyst", token: str | None = None,
                 role_id: str | None = None, secret_id: str | None = None, namespace: str | None = None,
                 http: httpx.Client | None = None, timeout: float = 10.0):
        self.url, self.mount, self.prefix = url.rstrip("/"), mount.strip("/"), prefix.strip("/")
        self._token, self._role_id, self._secret_id, self.namespace = token, role_id, secret_id, namespace
        self._token_expiry = float("inf") if token else 0.0
        self.http = http or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if time.time() >= self._token_expiry - 30:
            self._login()
        h = {"X-Vault-Token": self._token or ""}
        if self.namespace:
            h["X-Vault-Namespace"] = self.namespace
        return h

    def _login(self) -> None:
        if not (self._role_id and self._secret_id):
            raise SecretUnavailable("OpenBao: no token and no AppRole configured")
        r = self.http.post(f"{self.url}/v1/auth/approle/login", json={"role_id": self._role_id, "secret_id": self._secret_id})
        if r.status_code != 200:
            raise SecretUnavailable(f"OpenBao AppRole login failed ({r.status_code})")
        auth = r.json().get("auth") or {}
        self._token = auth.get("client_token")
        self._token_expiry = time.time() + float(auth.get("lease_duration") or 3600)

    def _path(self, ref: str) -> str:
        safe = hashlib.sha256(ref.encode()).hexdigest()[:40]  # references carry scope ids; paths stay opaque and tidy
        return f"{self.prefix}/{safe}"

    def store(self, ref: str, value: str) -> StoredRef:
        r = self.http.post(f"{self.url}/v1/{self.mount}/data/{self._path(ref)}", headers=self._headers(),
                           json={"data": {"value": value, "ref": ref}})
        if r.status_code not in (200, 204):
            raise SecretUnavailable(f"OpenBao refused the write ({r.status_code})")
        version = int(((r.json() if r.content else {}).get("data") or {}).get("version") or 1)
        return StoredRef(ref, self.name, version)

    def fetch(self, ref: str) -> str:
        r = self.http.get(f"{self.url}/v1/{self.mount}/data/{self._path(ref)}", headers=self._headers())
        if r.status_code == 404:
            raise SecretUnavailable("no such secret")
        if r.status_code == 403:
            raise SecretUnavailable("OpenBao denied the read: the service identity lacks the policy")
        if r.status_code != 200:
            raise SecretUnavailable(f"OpenBao unavailable ({r.status_code})")
        data = ((r.json().get("data") or {}).get("data") or {})
        if data.get("ref") != ref or "value" not in data:
            raise SecretUnavailable("OpenBao returned a record for another reference")
        return data["value"]

    def delete(self, ref: str) -> bool:
        r = self.http.delete(f"{self.url}/v1/{self.mount}/metadata/{self._path(ref)}", headers=self._headers())
        return r.status_code in (200, 204)

    def health(self) -> dict[str, Any]:
        try:
            r = self.http.get(f"{self.url}/v1/sys/health")
            body = r.json() if r.content else {}
            return {"backend": self.name, "ok": r.status_code in (200, 429, 472, 473), "sealed": body.get("sealed"),
                    "version": body.get("version")}
        except httpx.HTTPError as exc:
            return {"backend": self.name, "ok": False, "error": type(exc).__name__}


def fingerprint(value: str) -> str:
    """A stable, non-reversible identifier for a value (to detect duplicates and for audit), never the value."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


__all__ = ["EmbeddedBackend", "OpenBaoBackend", "SecretBackend", "SecretUnavailable", "StoredRef", "derive_kek",
           "fingerprint", "json"]
