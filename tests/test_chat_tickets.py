# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Chat tickets (``lyst.tickets``) and the browser's way to the chat routes: what a ticket can and cannot do, and
cross-origin access for the interface's origins only."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lyst import tickets
from lyst.api import aisdk, create_app

ROOT = Path(__file__).resolve().parents[1]


def test_a_ticket_is_signed_scoped_and_short_lived():
    t = tickets.issue("u_1", "northwind", now=1000)
    assert t["expires_at"] == 1000 + tickets.TTL and t["scheme"] == "Ticket"
    assert tickets.verify(t["ticket"], "northwind", now=1000 + tickets.TTL - 1) == "u_1"
    with pytest.raises(tickets.TicketInvalid, match="expired"):
        tickets.verify(t["ticket"], "northwind", now=1000 + tickets.TTL)
    with pytest.raises(tickets.TicketInvalid, match="another workspace"):
        tickets.verify(t["ticket"], "acme", now=1000)
    claims, sig = t["ticket"].split(".")
    forged = tickets._b64(json.dumps({"u": "u_admin", "w": "northwind", "x": 10**10, "n": "x"}).encode())
    for bad in (f"{forged}.{sig}", f"{claims}.{sig[:-2]}xx", "nonsense", "", "a.b.c"):
        with pytest.raises(tickets.TicketInvalid):
            tickets.verify(bad, "northwind", now=1000)
    other_key = tickets.issue("u_1", "northwind", now=1000, key=b"k" * 32)["ticket"]
    with pytest.raises(tickets.TicketInvalid):  # another engine's (or a restarted engine's) ticket
        tickets.verify(other_key, "northwind", now=1000)
    assert tickets.issue("u", "w", ttl=99_999, now=0)["expires_at"] == tickets.TTL  # never longer than TTL


def _body(text="What was revenue last quarter?", chat="c1"):
    return {"id": chat, "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": text}]}]}


def test_a_ticket_opens_the_chat_routes_and_nothing_else(seeded, monkeypatch):
    monkeypatch.setattr(aisdk, "POLL", 0.05)
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 'test')}"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.post("/v1/workspaces/northwind/chat/tickets", headers=h)
        assert r.status_code == 200 and r.json()["api"] == "/v1/workspaces/northwind/chat"
        th = {"Authorization": f"Ticket {r.json()['ticket']}"}
        threading.Thread(target=lambda: (time.sleep(0.2), lyst.runtime.drain()), daemon=True).start()
        s = c.post("/v1/workspaces/northwind/chat", headers=th, json=_body())
        assert s.status_code == 200 and s.text.endswith("data: [DONE]\n\n")
        task_id = json.loads(s.text.split("\n", 1)[0][6:])["messageId"]
        assert lyst.runtime.get(owner, task_id)["created_by"] == owner.user_id  # it acted as the person who holds it
        assert c.get("/v1/workspaces/northwind/chat/c1/stream", headers=th).status_code == 204
        # not a session: every other route refuses it, including issuing another ticket
        for path in ("/v1/workspaces/northwind/tasks", f"/v1/tasks/{task_id}", "/v1/workspaces/northwind/datasets"):
            assert c.get(path, headers=th).status_code == 403
        assert c.post("/v1/workspaces/northwind/chat/tickets", headers=th).status_code == 403
        assert c.post("/v1/workspaces/northwind/tasks", headers=th, json={"question": "x"}).status_code == 403
        # only for its workspace, and only while it is valid
        assert c.post("/v1/workspaces/acme/chat", headers=th, json=_body()).status_code == 401
        assert c.post("/v1/workspaces/northwind/chat", headers={"Authorization": "Ticket x.y"}, json=_body()).status_code == 401
        # a person removed from the workspace loses access at once, ticket or not
        vt = c.post("/v1/workspaces/northwind/chat/tickets",
                    headers={"Authorization": f"Bearer {lyst.guard.issue_token(viewer, 'test')}"}).json()["ticket"]
        lyst.guard.remove_member(owner, "northwind", viewer.user_id)
        assert c.post("/v1/workspaces/northwind/chat", headers={"Authorization": f"Ticket {vt}"},
                      json=_body(chat="c2")).status_code == 403
        # outsiders get no ticket
        out = lyst.guard.create_user("eve@elsewhere.test", "Eve")
        assert c.post("/v1/workspaces/northwind/chat/tickets",
                      headers={"Authorization": f"Bearer {lyst.guard.issue_token(out, 'test')}"}).status_code == 403


def test_only_the_interface_may_call_the_chat_routes_cross_origin(seeded, monkeypatch):
    monkeypatch.setenv("LYST_UI_ORIGINS", "http://lyst.localhost:8501, https://lyst.example.com")
    lyst, owner = seeded
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, content-type"}
    with TestClient(create_app(lyst, start_workers=False)) as c:
        r = c.options("/v1/workspaces/northwind/chat", headers={"Origin": "https://lyst.example.com", **pre})
        assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "https://lyst.example.com"
        assert "authorization" in r.headers["access-control-allow-headers"]
        assert "x-vercel-ai-ui-message-stream" in r.headers["access-control-expose-headers"]
        r = c.options("/v1/workspaces/northwind/chat/c1/stream", headers={"Origin": "http://lyst.localhost:8501", **pre})
        assert r.headers.get("access-control-allow-origin") == "http://lyst.localhost:8501"
        # another site, or any other route, gets no cross-origin access
        r = c.options("/v1/workspaces/northwind/chat", headers={"Origin": "https://evil.example", **pre})
        assert "access-control-allow-origin" not in r.headers
        r = c.options("/v1/workspaces/northwind/tasks", headers={"Origin": "https://lyst.example.com", **pre})
        assert "access-control-allow-origin" not in r.headers
        r = c.post("/v1/workspaces/northwind/chat", headers={"Origin": "https://lyst.example.com"}, json=_body())
        assert r.status_code == 403 and r.headers["access-control-allow-origin"] == "https://lyst.example.com"
    monkeypatch.delenv("LYST_UI_ORIGINS")
    monkeypatch.setenv("LYST_UI_PORT", "9000")
    assert aisdk.ui_origins() == {"http://lyst.localhost:9000", "http://localhost:9000", "http://127.0.0.1:9000"}


def test_the_bundle_is_built_and_its_licenses_are_listed():
    """The live answer ships prebuilt (no Node to install Lyst); its third-party licenses ship beside it."""
    here = ROOT / "src" / "lyst" / "ui" / "live_component"
    js = (here / "live.js").read_text(encoding="utf-8")
    pinned = json.loads((ROOT / "ui-src" / "live" / "package.json").read_text())["dependencies"]["ai"]
    assert js.startswith(f"// Lyst live answer (AGPL-3.0-only). Bundles the AI SDK ai@{pinned} ")
    assert "export{" in js and "Ticket " in js
    notices = (here / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    listed = [line for line in notices.splitlines() if line.endswith(")") and " (" in line]
    assert any(line.startswith(f"ai {pinned} (Apache-2.0)") for line in listed)
    assert all(line.rsplit("(", 1)[1].rstrip(")") in {"Apache-2.0", "MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "0BSD"}
               for line in listed)
