# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
import json

import httpx

from lyst.analyst.engine import unbacked_numbers
from lyst.config import LLMSettings
from lyst.llm import LLMClient


def test_unbacked_numbers():
    assert unbacked_numbers("Margin fell to 32.5% from 36.0%.", [0.325, 0.36]) == []
    assert unbacked_numbers("Margin fell to 31.2%.", [0.325]) == [0.312]
    assert unbacked_numbers("Revenue was $25.41M.", [25_410_000.0]) == []


def test_external_model_is_blocked_until_allowed_and_then_redacted(seeded):
    lyst, owner = seeded
    sent = []

    def handler(request: httpx.Request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    client = LLMClient(LLMSettings("https://llm.example/v1", "m", None, external=True), lyst.stores,
                       transport=httpx.MockTransport(handler))
    assert not client.allowed_for("northwind")
    lyst.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
    assert client.allowed_for("northwind")
    client.chat("northwind", [{"role": "user", "content": "email avery@northwind.test"}], purpose="t")
    assert "avery@northwind.test" not in sent[0]["messages"][0]["content"]
    assert lyst.stores.control.scalar("SELECT count(*) FROM llm_calls WHERE external") == 1


def test_model_narrative_with_invented_numbers_is_rejected(seeded):
    lyst, owner = seeded

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if body.get("response_format"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Margin collapsed by 47.3%."}}]})

    lyst.services.llm = LLMClient(LLMSettings("http://localhost:11434/v1", "local"), lyst.stores,
                                  transport=httpx.MockTransport(handler))
    from .conftest import ask

    r = ask(lyst, owner, "Why did operating margin decline last quarter?")["result"]
    assert "47.3" not in r["answer"] and r["supporting"]["narrative_by"].startswith("template (model wording dropped")


def test_openrouter_end_to_end_through_the_analyst(tmp_path, monkeypatch):
    """A workspace on OpenRouter: the analyst's model calls go through the gateway, are recorded with
    workload, pool and cost, and numbers in the model's narrative are still checked."""
    from lyst import demo
    from lyst.app import Lyst
    from lyst.config import load_settings

    monkeypatch.setenv("LYST_MASTER_KEY", "cd" * 32)
    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LYST_LLM_TIER", "economy")
    bodies = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        bodies.append(body)
        text = "{}" if body.get("response_format") else "EMEA drove the decline in operating margin."
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "provider": "Stub",
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 9, "cost": 0.00002}})

    app = Lyst.build(load_settings(tmp_path / "h"))
    app.services.llm = LLMClient(app.settings.llm, app.stores, transport=httpx.MockTransport(handler), credentials=app.credentials)
    try:
        assert app.settings.llm.provider == "openrouter"
        owner = app.guard.create_user("o@x.test", "O")
        demo.seed(app, owner)
        assert app.stores.control.scalar("SELECT allow_external_llm FROM workspaces WHERE id = 'northwind'")
        tid = app.runtime.submit(owner, "northwind", "Why did operating margin decline last quarter?")
        app.runtime.drain()
        r = app.runtime.get(owner, tid)["result"]
        assert r["answer"] == "EMEA drove the decline in operating margin."
        assert r["supporting"]["narrative_by"] == "model (numbers verified)"
        assert bodies[0]["model"] == "anthropic/claude-haiku-5.5"  # first economy model in the catalog
        rows = app.stores.control.query("SELECT workload, pool, provider, cost_usd, outcome FROM llm_calls")
        assert rows == [{"workload": "chat", "pool": "interactive", "provider": "Stub", "cost_usd": 0.00002,
                         "outcome": "ok"}]
        # only findings were sent, never dataset rows (the first month of data never appears)
        sent = json.dumps(bodies)
        assert "2024-01-01" not in sent
    finally:
        app.close()


def test_call_identity_is_cleared_accounting_never_fails_a_call_and_errors_say_why(seeded, monkeypatch):
    import pytest

    from lyst.llm import LLMResponseInvalid, LLMUnavailable

    lyst, owner = seeded
    handler = lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}], "usage": {}})  # noqa: E731
    client = LLMClient(LLMSettings("http://localhost:11434/v1", "local"), lyst.stores, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_owner_of", lambda task_id: "u_someone")
    monkeypatch.setattr(client, "_insert_call", lambda r, cost: (_ for _ in ()).throw(RuntimeError("store down")))
    assert client.chat("northwind", [{"role": "user", "content": "hi"}], purpose="t", task_id="t_1") == "not json"
    assert getattr(client._local, "user_id", None) is None  # the task's owner does not outlive the call
    with pytest.raises(LLMResponseInvalid):
        client.chat_json("northwind", [{"role": "user", "content": "hi"}], purpose="t")
    assert issubclass(LLMResponseInvalid, LLMUnavailable)
    # a local provider with no model configured has no model to describe, rather than an IndexError
    assert LLMClient(LLMSettings("http://localhost:11434/v1", None), lyst.stores, transport=httpx.MockTransport(handler)).info() is None
