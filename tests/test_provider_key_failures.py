# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Scenario 4 for the model provider: a key the provider rejects (revoked, expired, malformed) fails safely."""

import json

import httpx

from lyst.config import LLMSettings
from lyst.llm import LLMClient

from .conftest import ask

KEY = "sk-or-v1-" + "f" * 64


def test_a_rejected_provider_key_fails_safely(seeded):
    lyst, owner = seeded
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": f"No auth credentials found for {KEY}", "code": 401}})

    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    lyst.services.llm = LLMClient(LLMSettings("https://openrouter.example/api/v1", "m", KEY, external=True),
                                  lyst.stores, transport=httpx.MockTransport(handler))
    t = ask(lyst, owner, "Why did operating margin decline last quarter?")
    r = t["result"]
    # the analysis still completes on Lyst's own computation, and says why the model was not used
    assert t["status"] == "done" and r["confidence"]["level"] == "verified"
    assert r["supporting"]["narrative_by"].startswith("template (model unavailable")
    # a key the provider rejects is not retried across the task: a bounded number of calls
    print("provider calls with a rejected key:", len(calls))
    assert len(calls) <= 3, calls
    # and the key appears nowhere Lyst keeps or returns text
    stored = json.dumps([lyst.stores.control.query(f"SELECT * FROM {tb}") for tb in
                         ("llm_calls", "task_events", "audit_log", "tasks", "task_steps")], default=str)
    assert KEY not in stored and KEY not in json.dumps(t, default=str)
