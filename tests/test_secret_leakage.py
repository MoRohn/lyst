# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario 4: a connector whose errors echo its credential must not leak it anywhere Lyst stores or shows text."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from lyst.api import create_app
from lyst.connectors import CONNECTORS, Connector, ConnectorError, Field, Manifest

PASSWORD = "hunter2-Plain-Db-Pass"  # matches none of the redaction patterns: only exact-value scrubbing catches it


class Echo(Connector):
    """Like a real driver that puts its DSN or URL (with the credential) into its error text."""

    manifest = Manifest(kind="echo", label="Echo", fields=(
        Field("host", "Host"), Field("password", "Password", secret=True)), capabilities=("full",))
    fail = False

    def test(self):
        if Echo.fail:
            raise OSError(f"could not connect to postgresql://app:{self.secrets['password']}@{self.config['host']}/db "
                          f"(url-encoded: hunter2-Plain-Db-Pass%21)")
        return {"ok": True}

    def discover(self):
        raise RuntimeError(f"denied for password={self.secrets['password']}")

    def read(self, table, **kw):
        raise RuntimeError(f"read failed using {self.secrets['password']}")
        yield  # pragma: no cover


@pytest.fixture
def echo(monkeypatch):
    monkeypatch.setitem(CONNECTORS, "echo", Echo)
    Echo.fail = False
    return Echo


def test_connector_errors_never_carry_the_credential(seeded, echo, caplog):
    lyst, owner = seeded
    caplog.set_level(logging.DEBUG)
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    with TestClient(create_app(lyst, start_workers=False), raise_server_exceptions=False) as c:
        r = c.post("/v1/workspaces/northwind/connections", headers=h,
                   json={"kind": "echo", "name": "Echo", "values": {"host": "db.internal", "password": PASSWORD}})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        Echo.fail = True
        bodies = [c.post(f"/v1/workspaces/northwind/connections/{cid}/test", headers=h).text,
                  c.get(f"/v1/workspaces/northwind/connections/{cid}/tables", headers=h).text,
                  c.post(f"/v1/workspaces/northwind/connections/{cid}/sync", headers=h, json={"source_table": "t"}).text,
                  c.get("/v1/workspaces/northwind/connections", headers=h).text,
                  c.get("/v1/workspaces/northwind/audit", headers=h).text,
                  c.get("/v1/credentials", headers=h).text]
    assert "could not connect" in bodies[0] and "[secret]" in bodies[0]  # the reason survives, the value does not
    stored = json.dumps(lyst.stores.control.query("SELECT * FROM connections"), default=str)
    events = json.dumps(lyst.stores.control.query("SELECT * FROM credential_events"), default=str)
    for where, text in [*(("api", b) for b in bodies), ("connections row", stored), ("credential events", events),
                        ("logs", caplog.text)]:
        assert PASSWORD not in text, where
        assert "hunter2-Plain-Db-Pass%21" not in text and "hunter2" not in text, where


def test_a_constructor_that_echoes_its_input_is_scrubbed(seeded, monkeypatch):
    lyst, owner = seeded

    class Strict(Echo):
        manifest = Manifest(kind="strict", label="Strict", fields=Echo.manifest.fields)

        def __init__(self, config, secrets):
            raise ValueError(f"bad password {secrets['password']!r}")

    monkeypatch.setitem(CONNECTORS, "strict", Strict)
    with pytest.raises(ConnectorError) as err:
        lyst.connections.create(owner, "northwind", kind="strict", name="S", values={"host": "h", "password": PASSWORD})
    assert PASSWORD not in str(err.value) and err.value.__cause__ is None
