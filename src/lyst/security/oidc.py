# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""OIDC sign-in (authorization code flow with PKCE) against any compliant provider: Keycloak,
Authentik, Dex, Zitadel, or a commercial IdP. Lyst keeps no passwords.

LYST_OIDC_ISSUER         e.g. https://id.example.com/realms/lyst  (discovery at /.well-known/openid-configuration)
LYST_OIDC_CLIENT_ID
LYST_OIDC_CLIENT_SECRET  optional for public clients using PKCE only
LYST_OIDC_REDIRECT_URL   this API's callback, e.g. https://lyst.example.com/api/auth/callback
LYST_OIDC_ALLOWED_DOMAINS  optional comma list; only these email domains may sign in

The callback verifies the ID token's signature against the provider's JWKS, its issuer, audience,
expiry and nonce, then finds or creates the user by verified email and issues a Lyst session token
(an ordinary ``api_tokens`` row, labelled ``oidc``, expiring after ``LYST_SESSION_HOURS``).
Workspace membership is granted by workspace admins, never by signing in.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str | None
    redirect_url: str
    allowed_domains: tuple[str, ...] = ()
    session_hours: int = 12

    @classmethod
    def from_env(cls) -> OIDCSettings | None:
        env = os.environ
        if not env.get("LYST_OIDC_ISSUER"):
            return None
        from .vault import anchor  # noqa: PLC0415

        return cls(env["LYST_OIDC_ISSUER"].rstrip("/"), env["LYST_OIDC_CLIENT_ID"], anchor("LYST_OIDC_CLIENT_SECRET"),
                   env["LYST_OIDC_REDIRECT_URL"],
                   tuple(d.strip().lower() for d in env.get("LYST_OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()),
                   int(env.get("LYST_SESSION_HOURS", "12")))


def oidc_settings_from_credentials(credentials) -> OIDCSettings | None:
    """A sign-in provider connected in Settings › Secrets (installation scope) instead of the environment."""
    env = os.environ
    got = credentials.use("oidc_client", purpose="sign-in", consumer="oidc")
    if not got:
        return None
    secret, lease = got
    c = credentials._get(lease.credential_id)  # noqa: SLF001 - same trust boundary
    import json  # noqa: PLC0415

    f = json.loads(c["fields"])
    redirect = env.get("LYST_OIDC_REDIRECT_URL") or (env.get("LYST_PUBLIC_URL", "http://127.0.0.1:8770").rstrip("/") + "/auth/callback")
    return OIDCSettings(f["issuer"].rstrip("/"), f["client_id"], secret, redirect,
                        tuple(d.strip().lower() for d in env.get("LYST_OIDC_ALLOWED_DOMAINS", "").split(",") if d.strip()),
                        int(env.get("LYST_SESSION_HOURS", "12")))


class OIDCError(PermissionError):
    pass


class OIDC:
    def __init__(self, settings: OIDCSettings, http: httpx.Client | None = None, jwk_client: PyJWKClient | None = None):
        self.settings = settings
        self.http = http or httpx.Client(timeout=10)
        self._meta: dict[str, Any] | None = None
        self._jwks = jwk_client
        self._pending: dict[str, tuple[float, str, str, str]] = {}  # state -> (expires, verifier, nonce, return_to)

    def metadata(self) -> dict[str, Any]:
        if self._meta is None:
            r = self.http.get(self.settings.issuer + "/.well-known/openid-configuration")
            r.raise_for_status()
            self._meta = r.json()
            if self._meta.get("issuer", "").rstrip("/") != self.settings.issuer:
                raise OIDCError("discovery document issuer does not match LYST_OIDC_ISSUER")
        return self._meta

    def jwks(self) -> PyJWKClient:
        if self._jwks is None:
            self._jwks = PyJWKClient(self.metadata()["jwks_uri"], cache_keys=True)
        return self._jwks

    def login_url(self, return_to: str) -> str:
        self._sweep()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._pending[state] = (time.time() + 600, verifier, nonce, return_to)
        params = {"response_type": "code", "client_id": self.settings.client_id, "redirect_uri": self.settings.redirect_url,
                  "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256"}
        return self.metadata()["authorization_endpoint"] + "?" + urlencode(params)

    def _sweep(self) -> None:
        now = time.time()
        for k in [k for k, v in self._pending.items() if v[0] < now]:
            del self._pending[k]

    def complete(self, state: str, code: str) -> tuple[dict[str, Any], str]:
        """Exchange the code; return (verified claims, return_to)."""
        self._sweep()
        pending = self._pending.pop(state, None)
        if not pending:
            raise OIDCError("unknown or expired sign-in state")
        _, verifier, nonce, return_to = pending
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.settings.redirect_url,
                "client_id": self.settings.client_id, "code_verifier": verifier}
        auth = (self.settings.client_id, self.settings.client_secret) if self.settings.client_secret else None
        r = self.http.post(self.metadata()["token_endpoint"], data=data, auth=auth)
        if r.status_code != 200:
            raise OIDCError(f"token exchange failed ({r.status_code})")
        id_token = r.json().get("id_token")
        if not id_token:
            raise OIDCError("the provider returned no ID token")
        key = self.jwks().get_signing_key_from_jwt(id_token).key
        try:
            claims = jwt.decode(id_token, key, algorithms=["RS256", "ES256", "PS256"], audience=self.settings.client_id,
                                issuer=self.settings.issuer, options={"require": ["exp", "iat", "sub"]})
        except jwt.PyJWTError as exc:
            raise OIDCError(f"ID token rejected: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise OIDCError("nonce mismatch")
        email = (claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is False:
            raise OIDCError("the provider did not supply a verified email")
        domain = email.rsplit("@", 1)[-1]
        if self.settings.allowed_domains and domain not in self.settings.allowed_domains:
            raise OIDCError(f"sign-in from {domain} is not allowed")
        return claims, return_to
