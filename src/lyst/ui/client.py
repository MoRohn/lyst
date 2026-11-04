# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The UI's only route to Lyst: the HTTP API. No DuckDB access from Streamlit."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import streamlit as st


class APIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def _token() -> str | None:
    if t := st.session_state.get("token"):
        return t
    if t := st.query_params.get("token"):
        # arriving from /auth/callback: keep the token in this session and drop it from the URL
        st.session_state["token"] = t
        del st.query_params["token"]
        return t
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

    def _do(self, method: str, path: str, **kw) -> Any:
        try:
            r = self.http.request(method, path, headers=self._h(), **kw)
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

    def patch(self, path: str, json: Any) -> Any:
        return self._do("PATCH", path, json=json)


def client() -> Client:
    return Client()


def workspace() -> str:
    return st.session_state.get("workspace", "")
