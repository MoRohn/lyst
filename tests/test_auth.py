# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""OIDC sign-in against a stub provider, token expiry, metrics and structured logging."""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt import PyJWK, PyJWKClient

from lyst.api import create_app
from lyst.security import AccessDenied
from lyst.security.oidc import OIDC, OIDCError, OIDCSettings

ISSUER = "https://id.test/realms/lyst"


class StubIdP:
    """Discovery, authorize, token and JWKS for one RSA key; records what the client sent."""

    def __init__(self, email="dana@northwind.test", verified=True, wrong_nonce=False, wrong_aud=False):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.email, self.verified, self.wrong_nonce, self.wrong_aud = email, verified, wrong_nonce, wrong_aud
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []

    def jwk_client(self):
        pub = self.key.public_key()
        jwk = PyJWK.from_dict({**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub)), "kid": "k1", "alg": "RS256", "use": "sig"})
        client = PyJWKClient.__new__(PyJWKClient)
        client.get_signing_key_from_jwt = lambda token: jwk  # type: ignore[method-assign]
        return client

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if req.url.path.endswith("/token"):
            form = parse_qs(req.content.decode())
            self.token_requests.append(form)
            code = form["code"][0]
            if code not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            info = self.codes.pop(code)
            now = int(time.time())
            claims = {"iss": ISSUER, "sub": "sub-" + self.email, "aud": "wrong" if self.wrong_aud else "lyst",
                      "exp": now + 300, "iat": now, "nonce": "bad" if self.wrong_nonce else info["nonce"],
                      "email": self.email, "email_verified": self.verified, "name": "Dana Ko"}
            pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
            return httpx.Response(200, json={"id_token": jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})})
        return httpx.Response(404)

    def authorize(self, url: str) -> tuple[str, str]:
        """What a browser would do at the provider: issue a code bound to the state and nonce."""
        q = parse_qs(urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
        code = "code-" + q["state"][0][:6]
        self.codes[code] = {"nonce": q["nonce"][0]}
        return q["state"][0], code


def _oidc(idp: StubIdP, **kw) -> OIDC:
    settings = OIDCSettings(ISSUER, "lyst", "secret", "http://api.test/auth/callback", **kw)
    return OIDC(settings, http=httpx.Client(transport=httpx.MockTransport(idp.handler)), jwk_client=idp.jwk_client())


def test_oidc_sign_in_creates_user_and_session_token(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_UI_URL", "http://ui.test")
    idp = StubIdP()
    with TestClient(create_app(lyst, start_workers=False, oidc=_oidc(idp))) as c:
        r = c.get("/auth/login", params={"return_to": "http://ui.test/?page=analyst"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith(f"{ISSUER}/auth?")
        state, code = idp.authorize(r.headers["location"])
        r = c.get("/auth/callback", params={"state": state, "code": code}, follow_redirects=False)
        assert r.status_code == 302
        loc = urlparse(r.headers["location"])
        assert loc.netloc == "ui.test"
        q = parse_qs(loc.query)
        assert "token" not in q  # the session token never travels in a URL
        token = c.post("/auth/exchange", json={"code": q["login_code"][0]}).json()["token"]
        replay = c.post("/auth/exchange", json={"code": q["login_code"][0]})
        assert replay.status_code == 401 and replay.json()["error"] == "unauthorized"  # one use only
        assert idp.token_requests[0]["code_verifier"]  # PKCE verifier was sent
        me = c.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["email"] == "dana@northwind.test" and me["workspaces"] == []  # no membership from signing in
        # an owner adds her by email; she then sees the workspace
        ho = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
        assert c.post("/v1/workspaces/northwind/members", json={"email": "dana@northwind.test", "role": "editor"},
                      headers=ho).status_code == 200
        assert c.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()["workspaces"][0]["role"] == "editor"
        # the state cannot be replayed
        assert c.get("/auth/callback", params={"state": state, "code": code}, follow_redirects=False).status_code == 403
        # logout revokes the session
        assert c.post("/auth/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        assert c.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert lyst.stores.control.scalar("SELECT count(*) FROM audit_log WHERE action = 'auth.oidc' AND outcome = 'ok'") == 1


@pytest.mark.parametrize("kw,reason", [
    ({"wrong_nonce": True}, "nonce"), ({"wrong_aud": True}, "ID token rejected"),
    ({"verified": False}, "verified email")])
def test_oidc_rejects_bad_tokens(kw, reason):
    idp = StubIdP(**kw)
    o = _oidc(idp)
    state, code = idp.authorize(o.login_url("http://ui.test"))
    with pytest.raises(OIDCError, match=reason):
        o.complete(state, code)


def test_oidc_domain_allow_list():
    idp = StubIdP(email="eve@elsewhere.test")
    o = _oidc(idp, allowed_domains=("northwind.test",))
    state, code = idp.authorize(o.login_url("http://ui.test"))
    with pytest.raises(OIDCError, match="not allowed"):
        o.complete(state, code)


def test_expiring_tokens(seeded):
    lyst, owner = seeded
    token = lyst.guard.issue_token(owner, "short", hours=1)
    assert lyst.guard.authenticate(token).user_id == owner.user_id
    lyst.stores.control.execute("UPDATE api_tokens SET expires_at = now() - INTERVAL 1 MINUTE WHERE label = 'short'")
    with pytest.raises(AccessDenied):
        lyst.guard.authenticate(token)


def test_member_management_rules(seeded):
    lyst, owner = seeded
    admin = lyst.guard.create_user("ad@northwind.test", "Ad")
    lyst.guard.add_member(owner, "northwind", admin.user_id, "admin")
    other = lyst.guard.create_user("ot@northwind.test", "Ot")
    with pytest.raises(AccessDenied):  # an admin cannot mint owners
        lyst.guard.add_member(admin, "northwind", other.user_id, "owner")
    lyst.guard.add_member(admin, "northwind", other.user_id, "viewer")
    with pytest.raises(ValueError):  # the last owner stays
        lyst.guard.remove_member(owner, "northwind", owner.user_id)
    lyst.guard.remove_member(admin, "northwind", other.user_id)
    assert {m["email"] for m in lyst.guard.members(owner, "northwind")} == {"avery@northwind.test", "ad@northwind.test"}


def test_metrics_and_request_ids(seeded):
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.get("/v1/workspaces/northwind/datasets", headers=h)
        assert len(r.headers["x-request-id"]) == 16
        r = c.get("/v1/workspaces/northwind/datasets", headers={**h, "X-Request-Id": "abc123"})
        assert r.headers["x-request-id"] == "abc123"
        body = c.get("/metrics").text
        assert 'lyst_http_requests_total{method="GET",route="/v1/workspaces/{ws}/datasets",status="200"} 2.0' in body
        assert "lyst_tasks_queued" in body and "lyst_workers" in body
        # errors share one envelope, whether a handler raised HTTPException or a domain error
        nf = c.get("/auth/login")
        assert nf.status_code == 404 and set(nf.json()) == {"error", "detail"} and nf.json()["error"] == "not_found"
        # with a metrics token configured, a scrape must present it
        import os
        os.environ["LYST_METRICS_TOKEN"] = "scrape-secret"
        try:
            assert c.get("/metrics").status_code == 401
            assert c.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 200
        finally:
            del os.environ["LYST_METRICS_TOKEN"]


def test_metrics_are_served_in_setup_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home"))
    app = create_app(start_workers=False)
    app.state.lyst = None
    with TestClient(app) as c:
        c.app.state.lyst = None
        assert c.get("/metrics").status_code == 200


def test_installation_patch_is_refused_at_the_boundary_for_non_admins(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("viewer@northwind.test", "Vi")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer)}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.patch("/v1/installation", json={"add_admin": viewer.user_id}, headers=h)
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
