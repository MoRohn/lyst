# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What Lyst knows about each kind of credential: its friendly name, the non-secret fields it needs, the
least-privileged call that proves it works, and whether Lyst can revoke it at the source.

A test reports success, an authentication failure, a permission restriction or a connectivity problem,
and never the credential or the provider's payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    secret_label: str
    fields: tuple[tuple[str, str, bool], ...] = ()   # (name, label, required)
    scopes: tuple[str, ...] = ("installation", "workspace")
    revocable_at_source: bool = False
    revocation_note: str = "Revoking here stops Lyst using it; revoke the key at the provider as well."
    short_lived: bool = False
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider("openrouter", "OpenRouter", "API key", scopes=("installation", "workspace", "user"),
                           revocation_note="OpenRouter keys are revoked in the OpenRouter dashboard; revoking here only stops Lyst.",
                           notes="Models and embeddings on Acumane's rails. Install-wide by default; a workspace or personal key only when policy allows."),
    "local_llm": Provider("local_llm", "Local model server", "API key (optional)", fields=(("base_url", "Base URL", True),),
                          notes="An OpenAI-compatible server (Ollama, llama.cpp, vLLM)."),
    "postgres": Provider("postgres", "PostgreSQL", "Password", fields=(("host", "Host", True), ("port", "Port", False),
                                                                       ("dbname", "Database", True), ("user", "User", True),
                                                                       ("sslmode", "SSL mode", False)),
                         scopes=("workspace",), notes="Use a role with SELECT only."),
    "rest_token": Provider("rest_token", "REST API token", "Token", fields=(("url", "Endpoint URL", True), ("auth_header", "Header", False)),
                           scopes=("workspace",)),
    "oauth_client": Provider("oauth_client", "OAuth client (client credentials)", "Client secret",
                             fields=(("token_url", "Token URL", True), ("client_id", "Client ID", True), ("scope", "Scope", False),
                                     ("audience", "Audience", False)),
                             scopes=("workspace",), short_lived=True,
                             notes="Lyst exchanges the client secret for short-lived access tokens and never stores them."),
    "oidc_client": Provider("oidc_client", "Sign-in provider (OIDC client)", "Client secret", scopes=("installation",),
                            fields=(("issuer", "Issuer", True), ("client_id", "Client ID", True))),
    "webhook": Provider("webhook", "Notification webhook", "Signing secret (optional)", fields=(("url", "URL", True),),
                        scopes=("workspace",)),
    "custom": Provider("custom", "Custom secret", "Value", scopes=("installation", "workspace", "user"),
                       notes="Referenced by name from connectors and skills; Lyst does not test it."),
}


def _outcome(status: int) -> tuple[bool, str]:
    if status == 200:
        return True, "ok"
    if status in (401,):
        return False, "authentication failed"
    if status in (402, 403):
        return False, "permission restricted"
    if status >= 500:
        return False, "provider error"
    return False, f"unexpected response ({status})"


def test_credential(provider: str, secret: str, fields: dict[str, Any], *, http: httpx.Client | None = None) -> dict[str, Any]:
    """The least-privileged validation call for a provider. Returns {ok, outcome, detail?} only."""
    client = http or httpx.Client(timeout=10)
    try:
        if provider == "openrouter":
            r = client.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {secret}"})
            ok, outcome = _outcome(r.status_code)
            detail = {}
            if ok:
                d = (r.json().get("data") or {})
                detail = {"label": d.get("label"), "limit": d.get("limit"), "usage": d.get("usage")}
            return {"ok": ok, "outcome": outcome, **detail}
        if provider == "local_llm":
            r = client.get(fields["base_url"].rstrip("/") + "/models", headers={"Authorization": f"Bearer {secret}"} if secret else {})
            ok, outcome = _outcome(r.status_code)
            return {"ok": ok, "outcome": outcome}
        if provider == "postgres":
            import psycopg  # noqa: PLC0415

            try:
                with psycopg.connect(host=fields["host"], port=int(fields.get("port") or 5432), dbname=fields["dbname"], user=fields["user"],
                                     password=secret or None, sslmode=fields.get("sslmode") or "prefer", connect_timeout=5) as conn:
                    conn.read_only = True
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                return {"ok": True, "outcome": "ok"}
            except psycopg.OperationalError as exc:
                msg = str(exc).lower()
                return {"ok": False, "outcome": "authentication failed" if "password" in msg or "authentication" in msg else
                        ("connectivity problem" if "connect" in msg or "timeout" in msg or "refused" in msg else "provider error")}
        if provider == "rest_token":
            name = fields.get("auth_header") or "Authorization"
            headers = {name: f"Bearer {secret}" if name.lower() == "authorization" else secret}
            r = client.get(fields["url"], headers=headers)
            ok, outcome = _outcome(r.status_code)
            return {"ok": ok, "outcome": outcome}
        if provider == "oauth_client":
            data = {"grant_type": "client_credentials", "client_id": fields["client_id"], "client_secret": secret}
            if fields.get("scope"):
                data["scope"] = fields["scope"]
            if fields.get("audience"):
                data["audience"] = fields["audience"]
            r = client.post(fields["token_url"], data=data)
            ok, outcome = _outcome(r.status_code)
            body = r.json() if ok and r.content else {}
            return {"ok": ok and bool(body.get("access_token")), "outcome": outcome, "expires_in": body.get("expires_in")}
        if provider == "oidc_client":
            r = client.get(fields["issuer"].rstrip("/") + "/.well-known/openid-configuration")
            ok, outcome = _outcome(r.status_code)
            return {"ok": ok, "outcome": outcome if ok else f"issuer discovery: {outcome}"}
        if provider == "webhook":
            r = client.post(fields["url"], json={"event": "lyst.test"}, headers={"X-Lyst-Signature": "test"} if secret else {})
            ok, outcome = _outcome(r.status_code if r.status_code != 204 else 200)
            return {"ok": ok, "outcome": outcome}
        return {"ok": bool(secret), "outcome": "stored" if secret else "empty", "note": "presence check only"}
    except httpx.TimeoutException:
        return {"ok": False, "outcome": "connectivity problem (timeout)"}
    except httpx.HTTPError as exc:
        return {"ok": False, "outcome": f"connectivity problem ({type(exc).__name__})"}
    except KeyError as exc:
        return {"ok": False, "outcome": f"missing field {exc}"}
    finally:
        if http is None:
            client.close()


def fetch_oauth_token(secret: str, fields: dict[str, Any], *, http: httpx.Client | None = None) -> dict[str, Any]:
    """Client-credentials grant: a short-lived access token Lyst uses and never stores."""
    client = http or httpx.Client(timeout=10)
    try:
        data = {"grant_type": "client_credentials", "client_id": fields["client_id"], "client_secret": secret}
        for k in ("scope", "audience"):
            if fields.get(k):
                data[k] = fields[k]
        r = client.post(fields["token_url"], data=data)
        if r.status_code != 200:
            raise PermissionError(f"token endpoint returned {r.status_code}")
        body = r.json()
        return {"access_token": body["access_token"], "expires_in": int(body.get("expires_in") or 300), "token_type": body.get("token_type", "Bearer")}
    finally:
        if http is None:
            client.close()
