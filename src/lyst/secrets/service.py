# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Users manage connections. Lyst manages secrets. A dedicated boundary protects the keys.

``CredentialService`` is that boundary. A *credential* is a friendly, scoped record (installation,
workspace or user) for a provider, with non-secret fields, an owner, a status and its last test; its
secret value lives in a backend under an opaque reference and is never returned to the API or the UI.

* **Administration** (connect, test, replace, revoke, history) is server-side RBAC: installation
  credentials by installation admins, workspace credentials by that workspace's owners, personal
  credentials by their user. Sensitive actions require recent strong authentication.
* **Resolution** is an explicit policy per provider: an installation credential is authoritative; a
  workspace or personal credential is used only when the installation policy allows that provider to
  be overridden at that level. One user's credential is never used for another user.
* **Brokering**: only trusted in-process consumers (the model gateway, connector workers, the OIDC
  client) obtain plaintext, by purpose, for the duration of the call; every grant is an access decision
  in the audit chain (deduplicated per hour). Agent capabilities, skills in the sandbox and MCP tools
  never see the store; they ask the service to *do* something with a credential, not to *read* it.
* **Rotation and revocation**: replacement validates the new value first; the previous blob is
  deleted (only metadata and audit stay); caches and leases are invalidated; where the provider
  cannot be told, the audit line says so.
* **Audit** is a hash chain: each event carries the hash of the previous one, so a deleted or altered
  line breaks verification.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..security import AccessDenied, Principal
from . import providers as prov
from .backends import SecretBackend, SecretUnavailable, fingerprint

log = logging.getLogger("lyst.credentials")
SCOPES = ("installation", "workspace", "user")
DEFAULT_POLICY = {"openrouter": {"workspace_override": False, "user_credentials": False},
                  "custom": {"workspace_override": True, "user_credentials": True}}
REAUTH_SECONDS = 15 * 60


class CredentialConflict(RuntimeError):
    """Two replacements of one credential raced; the later one changed nothing and can be retried (HTTP 409)."""


class ReauthRequired(AccessDenied):
    """The caller must sign in again before this action."""


@dataclass
class Lease:
    credential_id: str
    provider: str
    scope_kind: str
    scope_id: str | None
    version: int
    purpose: str
    expires_at: float


class CredentialService:
    def __init__(self, stores, guard, backend: SecretBackend, *, http=None):
        self.stores, self.guard, self.backend, self.http = stores, guard, backend, http
        self._cache: dict[str, tuple[int, str]] = {}      # credential id -> (version, plaintext)
        self._token_cache: dict[str, tuple[float, str]] = {}  # oauth: credential id -> (expiry, token)
        self._decisions: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self.redactor = Redactor()
        # set by ConnectionService: (credential_id, kind, new_secret) -> (payload dict, test result); re-tests through the connector
        self.connector_rotate = None

    # ------------------------------------------------------------------ installation admins

    def installation_admins(self) -> list[str]:
        rows = self.stores.control.query("SELECT user_id FROM installation_admins ORDER BY added_at")
        if rows:
            return [r["user_id"] for r in rows]
        first = self.stores.control.query("SELECT id FROM users ORDER BY created_at, id LIMIT 1")
        return [first[0]["id"]] if first else []

    def is_installation_admin(self, user: Principal) -> bool:
        return user.kind == "system" or user.user_id in self.installation_admins()

    def add_installation_admin(self, actor: Principal, user_id: str) -> None:
        self._require_admin(actor, "installation", None, sensitive=True)
        self.stores.control.execute("INSERT OR REPLACE INTO installation_admins (user_id, added_by, added_at) VALUES (?, ?, now())",
                                    [user_id, actor.user_id])
        self._event(actor, "admin.add", None, "installation", None, "ok", {"user": user_id})

    # ------------------------------------------------------------------------- authorization

    def _recent_auth(self, actor: Principal) -> bool:
        age = getattr(actor, "auth_age_seconds", None)
        label = getattr(actor, "token_label", None)
        if actor.kind == "system" or label in ("cli", "dev"):   # operator-issued tokens are the operator's own trust
            return True
        if age is None:  # not an API session: an in-process caller (CLI, seeding) already runs as the operator
            return True
        return age <= REAUTH_SECONDS

    def _require_admin(self, actor: Principal, scope_kind: str, scope_id: str | None, *, sensitive: bool) -> None:
        if scope_kind not in SCOPES:
            raise ValueError("scope must be installation, workspace or user")
        ok = False
        if scope_kind == "installation":
            ok = self.is_installation_admin(actor)
        elif scope_kind == "workspace":
            ok = scope_id is not None and self.guard.role(actor, scope_id) == "owner"
        else:
            ok = scope_id == actor.user_id
        if not ok:
            self._event(actor, "credential.denied", None, scope_kind, scope_id, "denied", {"reason": "not an administrator of this scope"})
            raise AccessDenied(f"{actor.email or actor.user_id} may not manage {scope_kind} credentials" + (f" of {scope_id}" if scope_id else ""))
        if sensitive and not self._recent_auth(actor):
            self._event(actor, "credential.denied", None, scope_kind, scope_id, "denied", {"reason": "recent authentication required"})
            raise ReauthRequired("sign in again to manage credentials (recent authentication is required)")

    # ------------------------------------------------------------------------------- writes

    def connect(self, actor: Principal, *, scope_kind: str, scope_id: str | None, provider: str, name: str, secret: str,
                fields: dict[str, Any] | None = None, test_first: bool = True) -> dict[str, Any]:
        self._require_admin(actor, scope_kind, scope_id, sensitive=True)
        p = prov.PROVIDERS.get(provider)
        if not p:
            raise ValueError(f"unknown provider {provider}")
        if scope_kind not in p.scopes:
            raise ValueError(f"{p.label} credentials cannot be {scope_kind}-scoped")
        fields = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
        for fname, flabel, required in p.fields:
            if required and not fields.get(fname):
                raise ValueError(f"{flabel} is required")
        secret = (secret or "").strip()
        if not secret and p.secret_optional:
            secret = ""
        elif len(secret) < 8:
            raise ValueError("the secret is too short to be real")
        test = prov.test_credential(provider, secret, fields, http=self.http) if test_first and provider != "custom" else {"ok": True, "outcome": "not tested"}
        if test_first and not test["ok"]:
            self._event(actor, "credential.connect", None, scope_kind, scope_id, "failed", {"provider": provider, "name": name, "test": test["outcome"]})
            raise ValueError(f"the credential did not pass its test: {test['outcome']}")
        cid = "cred_" + uuid.uuid4().hex[:10]
        ref = f"{scope_kind}:{scope_id or 'installation'}:{cid}:v1"
        stored = self.backend.store(ref, secret)
        self.redactor.register(secret)

        def tx(c):
            superseded = [r[0] for r in c.execute(
                "SELECT secret_ref FROM credentials WHERE scope_kind = ? AND coalesce(scope_id, '') = ? AND provider = ? AND name = ? "
                "AND status = 'active'", [scope_kind, scope_id or "", provider, name]).fetchall()]
            c.execute("UPDATE credentials SET status = 'replaced', replaced_at = now() WHERE scope_kind = ? AND coalesce(scope_id, '') = ? "
                      "AND provider = ? AND name = ? AND status = 'active'", [scope_kind, scope_id or "", provider, name])
            c.execute("INSERT INTO credentials (id, scope_kind, scope_id, provider, name, secret_ref, backend, key_version, fields, owner_id, "
                      "status, version, fingerprint, last_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                      [cid, scope_kind, scope_id, provider, name, stored.ref, stored.backend, stored.key_version, json.dumps(fields), actor.user_id,
                       fingerprint(secret), json.dumps({**test, "at": _now()}, default=str)])
            return superseded

        try:
            superseded = self.stores.control.write(tx)
        except BaseException:
            self.backend.delete(ref)  # nothing points at the new blob: never leave it behind
            raise
        for old in superseded or []:  # a replaced credential keeps its metadata, never its secret
            try:
                self.backend.delete(old)
            except Exception:  # noqa: BLE001 - the new credential is live; a stale blob is cleaned up by revoke
                log.warning("could not delete a superseded secret blob")
        self._event(actor, "credential.connect", cid, scope_kind, scope_id, "ok", {"provider": provider, "name": name, "test": test["outcome"]})
        return {"id": cid, "provider": provider, "name": name, "scope_kind": scope_kind, "scope_id": scope_id, "test": test}

    def replace(self, actor: Principal, credential_id: str, secret: str, *, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate the new value, then swap: the old blob is deleted, only its metadata stays."""
        c = self._get(credential_id)
        self._require_admin(actor, c["scope_kind"], c["scope_id"], sensitive=True)
        merged = {**json.loads(c["fields"]), **{k: v for k, v in (fields or {}).items() if v not in (None, "")}}
        secret = (secret or "").strip()
        if len(secret) < 8:
            raise ValueError("the secret is too short to be real")
        if c["provider"].startswith("connector:"):
            # a connection's secret: keep the payload shape (one JSON object of secret fields) and re-test through the connector
            if self.connector_rotate is None:
                raise ValueError("connection secrets are rotated through the connections service")
            payload, test = self.connector_rotate(credential_id, c["provider"][len("connector:"):], secret)
            secret = json.dumps(payload)
        else:
            test = prov.test_credential(c["provider"], secret, merged, http=self.http) if c["provider"] != "custom" else {"ok": True, "outcome": "not tested"}
        if not test["ok"]:
            self._event(actor, "credential.replace", credential_id, c["scope_kind"], c["scope_id"], "failed", {"test": test["outcome"]})
            raise ValueError(f"the new credential did not pass its test: {test['outcome']}")
        new_version = int(c["version"]) + 1
        new_ref = f"{c['scope_kind']}:{c['scope_id'] or 'installation'}:{credential_id}:v{new_version}"
        stored = self.backend.store(new_ref, secret)
        old_ref = c["secret_ref"]

        def tx(cur):
            # concurrent replacements: the version must still be what we read, or this write loses
            row = cur.execute("SELECT version FROM credentials WHERE id = ?", [credential_id]).fetchall()[0]
            if int(row[0]) != int(c["version"]):
                raise CredentialConflict("the credential was replaced concurrently; retry")
            cur.execute("UPDATE credentials SET secret_ref = ?, backend = ?, key_version = ?, fields = ?, version = ?, fingerprint = ?, "
                        "last_test = ?, replaced_at = now(), status = 'active' WHERE id = ?",
                        [stored.ref, stored.backend, stored.key_version, json.dumps(merged), new_version, fingerprint(secret),
                         json.dumps({**test, "at": _now()}, default=str), credential_id])

        try:
            self.stores.control.write(tx)
        except BaseException:  # any failure, not only the version conflict: the new blob must not outlive it
            self.backend.delete(new_ref)
            raise
        self.backend.delete(old_ref)
        self.redactor.register(secret)
        self._invalidate(credential_id)
        self._event(actor, "credential.replace", credential_id, c["scope_kind"], c["scope_id"], "ok", {"version": new_version, "test": test["outcome"]})
        return {"id": credential_id, "version": new_version, "test": test}

    def revoke(self, actor: Principal, credential_id: str) -> dict[str, Any]:
        c = self._get(credential_id)
        self._require_admin(actor, c["scope_kind"], c["scope_id"], sensitive=True)
        self.backend.delete(c["secret_ref"])
        self.stores.control.execute("UPDATE credentials SET status = 'revoked', revoked_at = now(), revoked_by = ? WHERE id = ?",
                                    [actor.user_id, credential_id])
        self._invalidate(credential_id)
        p = prov.PROVIDERS.get(c["provider"])
        note = p.revocation_note if p and not p.revocable_at_source else "revoked at the source as well"
        self._event(actor, "credential.revoke", credential_id, c["scope_kind"], c["scope_id"], "ok", {"provider_side": note})
        return {"id": credential_id, "revoked": True, "provider_side": note}

    def test(self, actor: Principal, credential_id: str) -> dict[str, Any]:
        c = self._get(credential_id)
        self._require_admin(actor, c["scope_kind"], c["scope_id"], sensitive=False)
        try:
            secret = self._plaintext(c)
        except SecretUnavailable as exc:
            result = {"ok": False, "outcome": str(exc)}
        else:
            result = prov.test_credential(c["provider"], secret, json.loads(c["fields"]), http=self.http) if c["provider"] != "custom" \
                else {"ok": True, "outcome": "stored"}
        result["at"] = _now()
        self.stores.control.execute("UPDATE credentials SET last_test = ? WHERE id = ?", [json.dumps(result, default=str), credential_id])
        self._event(actor, "credential.test", credential_id, c["scope_kind"], c["scope_id"], "ok" if result["ok"] else "failed",
                    {"outcome": result["outcome"]})
        return result

    # -------------------------------------------------------------------------------- reads

    def _get(self, credential_id: str) -> dict[str, Any]:
        rows = self.stores.control.query("SELECT * FROM credentials WHERE id = ?", [credential_id])
        if not rows:
            raise KeyError(credential_id)
        return rows[0]

    def list(self, actor: Principal, *, scope_kind: str, scope_id: str | None = None) -> list[dict[str, Any]]:
        self._require_admin(actor, scope_kind, scope_id, sensitive=False)
        rows = self.stores.control.query(
            "SELECT c.id, c.provider, c.name, c.scope_kind, c.scope_id, c.fields, c.status, c.version, c.backend, c.created_at, c.replaced_at, "
            "c.revoked_at, c.last_test, u.email AS owner FROM credentials c LEFT JOIN users u ON u.id = c.owner_id "
            "WHERE c.scope_kind = ? AND coalesce(c.scope_id, '') = ? ORDER BY c.status, c.provider, c.name",
            [scope_kind, scope_id or ""])
        for r in rows:
            r["fields"] = json.loads(r["fields"])
            r["last_test"] = json.loads(r["last_test"]) if r["last_test"] else None
            p = prov.PROVIDERS.get(r["provider"])
            r["provider_label"] = p.label if p else r["provider"]
            r["revocable_at_source"] = bool(p and p.revocable_at_source)
        return rows

    def history(self, actor: Principal, credential_id: str) -> list[dict[str, Any]]:
        c = self._get(credential_id)
        self._require_admin(actor, c["scope_kind"], c["scope_id"], sensitive=False)
        rows = self.stores.control.query("SELECT seq, occurred_at, actor, action, outcome, detail FROM credential_events WHERE credential_id = ? ORDER BY seq",
                                         [credential_id])
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        return rows

    # ------------------------------------------------------------------------------- policy

    def policy(self) -> dict[str, dict[str, bool]]:
        raw = self.stores.control.scalar("SELECT value FROM installation_settings WHERE key = 'CREDENTIAL_POLICY'")
        pol = {k: dict(v) for k, v in DEFAULT_POLICY.items()}
        if raw:
            for k, v in json.loads(raw).items():
                pol.setdefault(k, {}).update(v)
        return pol

    def set_policy(self, actor: Principal, provider: str, *, workspace_override: bool | None = None, user_credentials: bool | None = None) -> dict:
        self._require_admin(actor, "installation", None, sensitive=True)
        pol = self.policy()
        entry = pol.setdefault(provider, {"workspace_override": False, "user_credentials": False})
        if workspace_override is not None:
            entry["workspace_override"] = bool(workspace_override)
        if user_credentials is not None:
            entry["user_credentials"] = bool(user_credentials)
        self.stores.control.execute("INSERT OR REPLACE INTO installation_settings (key, value, set_by, updated_at) VALUES "
                                    "('CREDENTIAL_POLICY', ?, ?, now())", [json.dumps(pol), actor.user_id])
        self._event(actor, "policy.set", None, "installation", None, "ok", {"provider": provider, **entry})
        self._cache.clear()
        return pol

    def resolve(self, provider: str, *, workspace: str | None = None, user: str | None = None, name: str | None = None) -> dict[str, Any] | None:
        """The active credential that applies, by explicit policy. Installation first; a workspace or user
        credential only where the installation policy allows that provider to be overridden there."""
        pol = self.policy().get(provider, {"workspace_override": False, "user_credentials": False})

        def active(kind: str, sid: str | None) -> dict[str, Any] | None:
            sql = ("SELECT * FROM credentials WHERE provider = ? AND scope_kind = ? AND coalesce(scope_id, '') = ? AND status = 'active'"
                   + (" AND name = ?" if name else "") + " ORDER BY created_at DESC LIMIT 1")
            rows = self.stores.control.query(sql, [provider, kind, sid or ""] + ([name] if name else []))
            return rows[0] if rows else None

        if user and pol.get("user_credentials"):
            c = active("user", user)
            if c:
                return c
        if workspace and pol.get("workspace_override"):
            c = active("workspace", workspace)
            if c:
                return c
        c = active("installation", None)
        if c:
            return c
        # providers that only make sense per workspace (databases, APIs) have no installation level to fall back to
        if workspace and provider not in ("openrouter", "local_llm", "oidc_client"):
            return active("workspace", workspace)
        return None

    # ------------------------------------------------------------------------------- broker

    def _plaintext(self, c: dict[str, Any]) -> str:
        with self._lock:
            hit = self._cache.get(c["id"])
            if hit and hit[0] == int(c["version"]):
                return hit[1]
        value = self.backend.fetch(c["secret_ref"])
        with self._lock:
            self._cache[c["id"]] = (int(c["version"]), value)
        self.redactor.register(value)
        return value

    def _invalidate(self, credential_id: str) -> None:
        with self._lock:
            self._cache.pop(credential_id, None)
            self._token_cache.pop(credential_id, None)

    def use(self, provider: str, *, purpose: str, workspace: str | None = None, user: str | None = None, name: str | None = None,
            consumer: str = "engine") -> tuple[str, Lease] | None:
        """Trusted in-process consumers only. Returns (plaintext, lease) or None when nothing applies.
        Each distinct (credential, purpose) grant is an access decision in the audit chain, at most hourly."""
        c = self.resolve(provider, workspace=workspace, user=user, name=name)
        if not c:
            return None
        try:
            value = self._plaintext(c)
        except SecretUnavailable as exc:
            self._event(Principal("system", "system@lyst.local", kind="system"), "credential.access", c["id"], c["scope_kind"], c["scope_id"],
                        "failed", {"purpose": purpose, "consumer": consumer, "reason": str(exc)})
            raise
        key = (c["id"], f"{consumer}:{purpose}:{workspace or ''}")
        now = time.time()
        with self._lock:
            due = self._decisions.get(key, 0) < now - 3600
            if due:
                self._decisions[key] = now
        if due:
            self._event(Principal("system", "system@lyst.local", kind="system"), "credential.access", c["id"], c["scope_kind"], c["scope_id"],
                        "ok", {"purpose": purpose, "consumer": consumer, "workspace": workspace})
            self.stores.control.execute("UPDATE credentials SET last_used_at = now() WHERE id = ?", [c["id"]])
        lease = Lease(c["id"], provider, c["scope_kind"], c["scope_id"], int(c["version"]), purpose, now + 900)
        return value, lease

    def oauth_token(self, credential_id: str, *, purpose: str) -> str:
        """A short-lived access token from an OAuth client credential, cached until shortly before expiry."""
        c = self._get(credential_id)
        if c["provider"] != "oauth_client" or c["status"] != "active":
            raise SecretUnavailable("not an active OAuth client credential")
        with self._lock:
            hit = self._token_cache.get(credential_id)
            if hit and hit[0] > time.time() + 30:
                return hit[1]
        tok = prov.fetch_oauth_token(self._plaintext(c), json.loads(c["fields"]), http=self.http)
        with self._lock:
            self._token_cache[credential_id] = (time.time() + tok["expires_in"], tok["access_token"])
        self.redactor.register(tok["access_token"])
        self._event(Principal("system", "system@lyst.local", kind="system"), "credential.token", credential_id, c["scope_kind"], c["scope_id"],
                    "ok", {"purpose": purpose, "expires_in": tok["expires_in"]})
        return tok["access_token"]

    def on_membership_change(self, workspace: str, user_id: str) -> None:
        """A user who lost a workspace keeps no cached grant there; their personal credentials stay theirs.

        The cache only throttles the hourly access-decision audit line; dropping a workspace's entries makes the next
        use there record a fresh decision."""
        with self._lock:
            self._decisions = {k: v for k, v in self._decisions.items() if not k[1].endswith(f":{workspace}")}

    # ------------------------------------------------------------- connectors (trusted workers)

    def store_connector_secrets(self, actor: Principal, workspace: str, kind: str, name: str, secrets: dict[str, str]) -> str:
        """A connection's secret fields, sealed as one record owned by the workspace. Anyone who may create the
        connection (data.ingest) may store them; only the connector worker ever reads them back."""
        self.guard.require(actor, workspace, "data.ingest", name)
        cid = "cred_" + uuid.uuid4().hex[:10]
        ref = f"workspace:{workspace}:{cid}:v1"
        payload = json.dumps({k: v for k, v in secrets.items() if v not in (None, "")})
        stored = self.backend.store(ref, payload)
        for v in secrets.values():
            if v:
                self.redactor.register(v)
        self.stores.control.execute(
            "INSERT INTO credentials (id, scope_kind, scope_id, provider, name, secret_ref, backend, key_version, fields, owner_id, "
            "status, version, fingerprint, last_test) VALUES (?, 'workspace', ?, ?, ?, ?, ?, ?, '{}', ?, 'active', 1, ?, ?)",
            [cid, workspace, f"connector:{kind}", name, stored.ref, stored.backend, stored.key_version, actor.user_id, fingerprint(payload),
             json.dumps({"ok": True, "outcome": "connection tested", "at": _now()})])
        self._event(actor, "credential.connect", cid, "workspace", workspace, "ok", {"provider": f"connector:{kind}", "name": name})
        return cid

    def connector_secrets(self, credential_id: str | None, *, workspace: str, consumer: str = "connector") -> dict[str, str]:
        """Plaintext for the connector worker, bound to the workspace that owns the record."""
        if not credential_id:
            return {}
        c = self._get(credential_id)
        if c["scope_kind"] != "workspace" or c["scope_id"] != workspace:
            self._event(Principal("system", "system@lyst.local", kind="system"), "credential.denied", credential_id, "workspace", workspace,
                        "denied", {"reason": "credential belongs to another workspace", "consumer": consumer})
            raise SecretUnavailable("that credential belongs to another workspace")
        if c["status"] != "active":
            raise SecretUnavailable("the connection's credential was revoked")
        value = self._plaintext(c)
        if c["provider"].startswith("connector:"):
            return json.loads(value)
        # a provider credential (postgres, rest_token, oauth_client) attached to a connection: map it onto connector fields
        fields = json.loads(c["fields"])
        if c["provider"] == "postgres":
            return {**{k: fields[k] for k in ("host", "port", "dbname", "user", "sslmode") if fields.get(k)}, "password": value}
        if c["provider"] == "rest_token":
            return {**{k: fields[k] for k in ("url", "auth_header") if fields.get(k)}, "token": value}
        if c["provider"] == "oauth_client":
            return {"token": self.oauth_token(credential_id, purpose="connector")}
        return {"value": value}

    def delete_connector_secrets(self, actor: Principal, workspace: str, credential_id: str | None) -> None:
        if not credential_id:
            return
        c = self._get(credential_id)
        if c["scope_kind"] != "workspace" or c["scope_id"] != workspace or not c["provider"].startswith("connector:"):
            return  # a shared provider credential outlives the connection that used it
        self.guard.require(actor, workspace, "data.ingest")
        self.backend.delete(c["secret_ref"])
        self.stores.control.execute("UPDATE credentials SET status = 'revoked', revoked_at = now(), revoked_by = ? WHERE id = ?",
                                    [actor.user_id, credential_id])
        self._invalidate(credential_id)
        self._event(actor, "credential.revoke", credential_id, "workspace", workspace, "ok", {"reason": "connection deleted"})

    # ------------------------------------------------------------------ installation settings

    def setting(self, key: str) -> str | None:
        return self.stores.control.scalar("SELECT value FROM installation_settings WHERE key = ?", [key])

    def settings_all(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.stores.control.query(
            "SELECT key, value FROM installation_settings WHERE key <> 'CREDENTIAL_POLICY'")}

    def set_setting(self, actor: Principal, key: str, value: str | None) -> None:
        self._require_admin(actor, "installation", None, sensitive=False)
        if value in (None, ""):
            self.stores.control.execute("DELETE FROM installation_settings WHERE key = ?", [key])
        else:
            self.stores.control.execute("INSERT OR REPLACE INTO installation_settings (key, value, set_by, updated_at) VALUES (?, ?, ?, now())",
                                        [key, value, actor.user_id])
        self._event(actor, "settings.set", None, "installation", None, "ok", {"key": key, "value": value})

    # ------------------------------------------------------------------------ bootstrap seeds

    def seed_from_anchors(self, anchor) -> list[str]:
        """Unattended deployments can mount a provider key beside the master key; it becomes an installation
        credential once (audited as a seed) so there is one path for every consumer afterwards."""
        import os  # noqa: PLC0415

        seeded = []
        system = Principal("system", "system@lyst.local", kind="system")
        key = anchor("LYST_OPENROUTER_API_KEY")
        if key and not self.resolve("openrouter"):
            self.connect(system, scope_kind="installation", scope_id=None, provider="openrouter", name="OpenRouter (mounted)", secret=key,
                         test_first=False)
            seeded.append("openrouter")
        oidc = anchor("LYST_OIDC_CLIENT_SECRET")
        if oidc and os.environ.get("LYST_OIDC_ISSUER") and os.environ.get("LYST_OIDC_CLIENT_ID") and not self.resolve("oidc_client"):
            self.connect(system, scope_kind="installation", scope_id=None, provider="oidc_client", name="Sign-in (mounted)", secret=oidc,
                         fields={"issuer": os.environ["LYST_OIDC_ISSUER"], "client_id": os.environ["LYST_OIDC_CLIENT_ID"]}, test_first=False)
            seeded.append("oidc_client")
        return seeded

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend.health(), "admins": self.installation_admins(), "audit": self.verify_audit_chain(),
                "active": self.stores.control.scalar("SELECT count(*) FROM credentials WHERE status = 'active'") or 0}

    # -------------------------------------------------------------------------------- audit

    def _event(self, actor: Principal, action: str, credential_id: str | None, scope_kind: str, scope_id: str | None, outcome: str,
               detail: dict[str, Any]) -> None:
        payload = json.dumps(self.redactor.scrub(detail), default=str, sort_keys=True)

        def tx(c):
            prev = c.execute("SELECT hash FROM credential_events ORDER BY seq DESC LIMIT 1").fetchall()
            prev_hash = prev[0][0] if prev else "genesis"
            seq = c.execute("SELECT nextval('credential_event_seq')").fetchall()[0][0]
            at = _now()
            h = hashlib.sha256(f"{prev_hash}|{seq}|{at}|{actor.user_id}|{action}|{credential_id}|{scope_kind}|{scope_id}|{outcome}|{payload}".encode()).hexdigest()
            c.execute("INSERT INTO credential_events (seq, occurred_at, actor, action, credential_id, scope_kind, scope_id, outcome, detail, "
                      "prev_hash, hash) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      [seq, at, actor.user_id, action, credential_id, scope_kind, scope_id, outcome, payload, prev_hash, h])

        self.stores.control.write(tx)

    def verify_audit_chain(self, *, batch: int = 5000) -> dict[str, Any]:
        """Re-hash the chain in bounded batches (keyset on seq), carrying the previous hash across them."""
        prev, last_seq, count = "genesis", None, 0
        while True:
            rows = self.stores.control.query(
                "SELECT seq, occurred_at, actor, action, credential_id, scope_kind, scope_id, outcome, detail, prev_hash, hash "
                "FROM credential_events" + (" WHERE seq > ?" if last_seq is not None else "") + " ORDER BY seq LIMIT ?",
                ([last_seq] if last_seq is not None else []) + [batch])
            if not rows:
                break
            for r in rows:
                count += 1
                expected = hashlib.sha256(f"{prev}|{r['seq']}|{r['occurred_at']}|{r['actor']}|{r['action']}|{r['credential_id']}|"
                                          f"{r['scope_kind']}|{r['scope_id']}|{r['outcome']}|{r['detail']}".encode()).hexdigest()
                if r["prev_hash"] != prev or r["hash"] != expected:
                    total = self.stores.control.scalar("SELECT count(*) FROM credential_events") or 0
                    return {"ok": False, "broken_at": r["seq"], "events": int(total)}
                prev = r["hash"]
            last_seq = rows[-1]["seq"]
        return {"ok": True, "events": count, "head": prev}

    def events(self, actor: Principal, *, scope_kind: str, scope_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        self._require_admin(actor, scope_kind, scope_id, sensitive=False)
        rows = self.stores.control.query("SELECT seq, occurred_at, actor, action, credential_id, outcome, detail FROM credential_events WHERE scope_kind = ? "
                                         "AND coalesce(scope_id, '') = ? ORDER BY seq DESC LIMIT ?", [scope_kind, scope_id or "", limit])
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        return rows


class Redactor(logging.Filter):
    """Masks every plaintext the broker has released from log records and from audit details."""

    MAX_VALUES = 4096  # the most recently released plaintexts; an old one leaves only after thousands of newer ones

    def __init__(self) -> None:
        super().__init__()
        self._values: dict[str, None] = {}  # insertion-ordered: the oldest is evicted first
        self._pattern: re.Pattern[str] | None = None
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        if value and len(value) >= 6:
            with self._lock:
                if value in self._values:
                    self._values[value] = self._values.pop(value)  # most recent again
                    return
                self._values[value] = None
                while len(self._values) > self.MAX_VALUES:
                    self._values.pop(next(iter(self._values)))
                self._pattern = None

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            with self._lock:
                if self._pattern is None and self._values:
                    # one alternation, longest first, rebuilt only when the set changes
                    self._pattern = re.compile("|".join(re.escape(v) for v in sorted(self._values, key=len, reverse=True)))
                pattern = self._pattern
            return pattern.sub("[secret]", obj) if pattern is not None else obj
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.scrub(v) for v in obj]
        return obj

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.scrub(str(record.getMessage()))
            record.args = ()
            for k, v in list(record.__dict__.items()):
                if isinstance(v, str | dict | list) and k not in ("msg", "name", "pathname", "filename", "module", "funcName"):
                    record.__dict__[k] = self.scrub(v)
        except Exception:  # noqa: BLE001, S110 - never let redaction break logging
            pass
        return True

    def install(self) -> None:
        """Scrub every handler of the root logger (filters on a logger apply only to its own records)."""
        root = logging.getLogger()
        for h in root.handlers:
            if self not in h.filters:
                h.addFilter(self)
        if self not in root.filters:
            root.addFilter(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


__all__ = ["CredentialConflict", "CredentialService", "Lease", "ReauthRequired", "Redactor", "SCOPES"]
