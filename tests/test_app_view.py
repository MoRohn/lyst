# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""A Lyst App in the page: a chat ticket opens generating and opening an app (and nothing else of the apps routes),
cross-origin only for the interface, and the shipped bundle draws the app in a locked-down sandbox."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from lyst.api import create_app

ROOT = Path(__file__).resolve().parents[1]
WS = "northwind"


def _body(text="An app for revenue by region", chat="a1"):
    return {"id": chat, "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": text}]}]}


def test_a_ticket_generates_and_opens_apps_and_nothing_else(seeded, monkeypatch):
    monkeypatch.setenv("LYST_UI_ORIGINS", "https://lyst.example.com")
    lyst, owner = seeded
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        t = c.post(f"/v1/workspaces/{WS}/chat/tickets", headers=h).json()
        assert t["apps"] == f"/v1/workspaces/{WS}/apps"
        th = {"Authorization": f"Ticket {t['ticket']}"}
        r = c.post(f"/v1/workspaces/{WS}/apps/generate", headers=th, json=_body())
        assert r.status_code == 200 and r.text.endswith("data: [DONE]\n\n")
        out = next(json.loads(line[6:]) for line in r.text.split("\n\n") if '"tool-output-available"' in line)["output"]
        app_id = out["appId"]
        assert out["saved"] and lyst.apps.get(owner, WS, app_id)["created_by"] == owner.user_id  # acted as its holder
        opened = c.get(f"/v1/workspaces/{WS}/apps/{app_id}/open", headers=th)
        assert opened.status_code == 200 and opened.json()["app_id"] == app_id
        # every other apps route refuses it: it is not a session
        assert c.get(f"/v1/workspaces/{WS}/apps", headers=th).status_code == 403
        assert c.get(f"/v1/workspaces/{WS}/apps/{app_id}", headers=th).status_code == 403
        assert c.post(f"/v1/workspaces/{WS}/apps", headers=th, json={"spec": {}}).status_code == 403
        assert c.post(f"/v1/workspaces/{WS}/apps/{app_id}/retire", headers=th).status_code == 403
        assert c.post("/v1/workspaces/acme/apps/generate", headers=th, json=_body()).status_code == 401
        # cross-origin: the interface only, and only on the two routes a ticket opens
        pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, content-type"}
        ok = c.options(f"/v1/workspaces/{WS}/apps/generate", headers={"Origin": "https://lyst.example.com", **pre})
        assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == "https://lyst.example.com"
        ok = c.options(f"/v1/workspaces/{WS}/apps/{app_id}/open", headers={"Origin": "https://lyst.example.com", **pre})
        assert ok.headers.get("access-control-allow-origin") == "https://lyst.example.com"
        for path in (f"/v1/workspaces/{WS}/apps", f"/v1/workspaces/{WS}/apps/{app_id}"):
            assert "access-control-allow-origin" not in c.options(path, headers={"Origin": "https://lyst.example.com", **pre}).headers
        assert "access-control-allow-origin" not in c.options(
            f"/v1/workspaces/{WS}/apps/generate", headers={"Origin": "https://evil.example", **pre}).headers


def test_the_app_is_drawn_in_a_locked_down_sandbox():
    """The renderer has no network, no same origin and never parses what it receives as HTML."""
    here = ROOT / "src" / "lyst" / "ui" / "apps_component"
    js = (here / "apps.js").read_text(encoding="utf-8")
    pinned = json.loads((ROOT / "ui-src" / "live" / "package.json").read_text())["dependencies"]["ai"]
    assert js.startswith(f"// Lyst app view (AGPL-3.0-only). Bundles the AI SDK ai@{pinned} ")
    assert '"sandbox","allow-scripts"' in js and "allow-same-origin" not in js
    assert "default-src 'none'" in js and "Ticket " in js
    frame = (ROOT / "ui-src" / "live" / "src" / "frame.js").read_text(encoding="utf-8")
    code = re.sub(r"^\s*//.*$", "", frame, flags=re.M)
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "fetch(", "XMLHttpRequest", "eval(",
                   "new Function", "localStorage", "WebSocket", "importScripts"):
        assert unsafe not in code, unsafe
    host = (ROOT / "ui-src" / "live" / "src" / "apps.js").read_text(encoding="utf-8")
    assert "e.source !== this.frame.contentWindow" in host  # only its own frame is listened to
    notices = (here / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    assert any(line.startswith(f"ai {pinned} (Apache-2.0)") for line in notices.splitlines())
