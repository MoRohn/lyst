# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Deployment profiles and diagnostics: defaults that fit the installation, and checks that say what to fix."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lyst import cli, diagnostics, profiles
from lyst.api import create_app
from lyst.webhooks import check_target


def test_profiles_fill_defaults_without_overriding():
    env = {"LYST_PROFILE": "offline", "LYST_CATALOG_AUTO": "1"}
    assert profiles.apply(env) == "offline"
    assert env["LYST_LLM_PROVIDER"] == "local" and env["LYST_OFFLINE"] == "1"
    assert env["LYST_CATALOG_AUTO"] == "1"  # an explicit setting wins
    with pytest.raises(ValueError, match="not a profile"):
        profiles.apply({"LYST_PROFILE": "cloudy"})
    assert profiles.apply({}) == "local"


def test_offline_webhooks_stay_inside(monkeypatch):
    monkeypatch.setenv("LYST_PROFILE", "offline")
    monkeypatch.setenv("LYST_OFFLINE", "1")
    with pytest.raises(ValueError, match="offline"):
        check_target("https://1.1.1.1/hook")
    check_target("http://10.0.0.5/hook")  # private, allowed


def test_diagnostics_report(seeded):
    lyst, owner = seeded
    report = diagnostics.run(lyst, network=False)
    by = {c["id"]: c for c in report["checks"]}
    assert report["status"] in ("healthy", "attention", "unhealthy")
    assert by["encryption"]["status"] == "ok" and by["schema"]["status"] == "ok" and by["credential_audit"]["status"] == "ok"
    assert by["python"]["area"] == "start" and by["data_folder"]["detail"] == "writable"  # from `lyst start --check`
    assert "database" not in by and "already_running" not in by
    assert by["backup"]["status"] == "warn" and "Settings › Advanced › Back up now" in by["backup"]["fix"]
    assert "lyst backup" in by["backup"]["fix"] and "Secrets & Connections" not in json.dumps(report)
    assert by["model"]["status"] == "warn"  # no OpenRouter key in tests
    text = json.dumps(report)
    assert "ab" * 32 not in text and str(lyst.settings.home) not in text  # no key, no local paths
    assert "lyst doctor" in diagnostics.render(report).lower()


def test_offline_profile_flags_internet_model_and_catalog(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_PROFILE", "offline")
    monkeypatch.setenv("LYST_CATALOG_AUTO", "1")
    by = {c["id"]: c for c in diagnostics.checks(lyst, network=False)}
    assert by["offline_model"]["status"] == "fail" and "OpenRouter" in by["offline_model"]["detail"]
    assert by["offline_catalog"]["status"] == "fail"


def test_exposed_without_identity_fails(seeded, monkeypatch):
    lyst, owner = seeded
    monkeypatch.setenv("LYST_HOST", "0.0.0.0")  # noqa: S104 - the case under test
    by = {c["id"]: c for c in diagnostics.checks(lyst, network=False)}
    assert by["exposure"]["status"] == "fail" and "OIDC" in by["exposure"]["fix"]


def test_diagnostics_api_is_for_installation_admins(seeded):
    lyst, owner = seeded
    viewer = lyst.guard.create_user("sam@northwind.test", "Sam")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer")
    with TestClient(create_app(lyst, start_workers=False)) as c:
        h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner, 't')}"}
        hv = {"Authorization": f"Bearer {lyst.guard.issue_token(viewer, 't')}"}
        assert c.get("/v1/diagnostics", headers=h, params={"network": "false"}).json()["checks"]
        assert c.get("/v1/diagnostics", headers=hv).status_code == 403


def test_doctor_cli_runs_locally_when_no_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LYST_MASTER_KEY", "cd" * 32)
    monkeypatch.setenv("LYST_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LYST_API_URL", "http://127.0.0.1:9")  # nothing listens there
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--no-network"])
    out = capsys.readouterr().out
    assert "Lyst doctor:" in out and "encrypted at rest" in out
    assert e.value.code in (0, 1)
