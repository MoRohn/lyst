# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The UI's only route to Lyst: the HTTP API. No DuckDB access from Streamlit."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import streamlit as st


class APIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def shared_identity_allowed(client_ip: str | None, env: dict[str, str], proxied: bool = False) -> bool:
    """Whether a browser may borrow the installation's shared token (LYST_TOKEN or the dev-token file) instead of
    signing in: only when the interface listens on loopback alone (LYST_UI_LOCAL=1, set by `lyst start` and
    `lyst ui`), the browser is on this machine, and no proxy forwarded the request. Otherwise anyone who could
    reach the port, on the network or through a proxy, would act as the token's owner.

    Streamlit reports a loopback client as ``None`` (st.context.ip_address), so None means this machine; the
    binding is the real control, since a loopback-only socket accepts nothing else."""
    if env.get("LYST_UI_LOCAL") != "1" or proxied:
        return False
    return client_ip is None or client_ip in LOOPBACK


def _client() -> tuple[str | None, bool]:
    try:
        headers = st.context.headers
        proxied = any(h in headers for h in ("X-Forwarded-For", "Forwarded", "X-Real-Ip"))
        ip = st.context.ip_address
        # a real session gives a string or None (loopback); anything else is a test harness with no request
        return (ip if isinstance(ip, str) else None), proxied
    except Exception:  # noqa: BLE001 - outside a browser session (bare mode)
        return None, False


def _token() -> str | None:
    if t := st.session_state.get("token"):
        return t
    if t := st.query_params.get("token"):
        # arriving from /auth/callback: keep the token in this session and drop it from the URL
        st.session_state["token"] = t
        del st.query_params["token"]
        return t
    ip, proxied = _client()
    if not shared_identity_allowed(ip, dict(os.environ), proxied=proxied):
        return None  # sign in (OIDC) or paste a personal token
    if t := os.environ.get("LYST_TOKEN"):
        return t
    home = Path(os.environ.get("LYST_HOME", "var"))
    f = home / "dev-token"
    return f.read_text().strip() if f.exists() else None


@st.cache_resource
def _http(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=30)


class Client:
    def __init__(self) -> None:
        self.base = os.environ.get("LYST_API_URL", "http://127.0.0.1:8770")
        self.http = _http(self.base)

    def _h(self) -> dict[str, str]:
        tok = _token()
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def _do(self, method: str, path: str, headers_extra: dict[str, str] | None = None, **kw) -> Any:
        try:
            r = self.http.request(method, path, headers=self._h() | (headers_extra or {}), **kw)
        except httpx.HTTPError as exc:
            raise APIError(0, f"Lyst's API is not reachable at {self.base} ({type(exc).__name__}).") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise APIError(r.status_code, str(detail))
        return r.json()

    def get(self, path: str, **params) -> Any:
        return self._do("GET", path, params=params or None)

    def post(self, path: str, json: Any | None = None, **kw) -> Any:
        return self._do("POST", path, json=json, **kw)

    def ask(self, ws: str, question: str) -> Any:
        """Submit a request once. A double click, a second rerun or a retry of the same request from this browser
        session within the same 30 seconds carries the same Idempotency-Key, so the engine returns the first task
        instead of running the work twice."""
        sid = st.session_state.setdefault("_lyst_sid", uuid.uuid4().hex)
        key = hashlib.sha256(f"{sid}|{ws}|{' '.join(question.split())}|{int(time.time() // 30)}".encode()).hexdigest()
        return self._do("POST", f"/v1/workspaces/{ws}/tasks", json={"question": question},
                        headers_extra={"Idempotency-Key": key})

    def patch(self, path: str, json: Any) -> Any:
        return self._do("PATCH", path, json=json)

    def delete(self, path: str) -> Any:
        return self._do("DELETE", path)


def client() -> Client:
    return Client()


def workspace() -> str:
    return st.session_state.get("workspace", "")
