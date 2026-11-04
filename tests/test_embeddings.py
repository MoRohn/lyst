# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Semantic recall: embeddings through the gateway, hybrid document search, memory recall by meaning."""

import json
import math

import httpx

from lyst import demo
from lyst.app import Lyst
from lyst.config import load_settings
from lyst.llm import LLMClient

# a stub "embedding model": a few concepts map to fixed directions, so semantically related texts are close
CONCEPTS = {"margin": 0, "profit": 0, "profitability": 0, "revenue": 1, "sales": 1, "turnover": 1, "supplier": 2,
            "vendor": 2, "procurement": 2, "emea": 3, "europe": 3, "staffing": 4, "headcount": 4, "agents": 4}


def fake_embed(text: str) -> list[float]:
    v = [0.0] * 6
    for w in text.lower().replace(",", " ").replace(".", " ").split():
        if w in CONCEPTS:
            v[CONCEPTS[w]] += 1.0
    v[5] = 0.1
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if request.url.path.endswith("/embeddings"):
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": fake_embed(t)} for i, t in enumerate(texts)],
                                         "usage": {"prompt_tokens": sum(len(t) // 4 for t in texts), "cost": 0.000001}})
    return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}})


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("LYST_MASTER_KEY", "ab" * 32)
    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LYST_EMBED_MODEL", "openai/text-embedding-3-small")
    app = Lyst.build(load_settings(tmp_path / "h"))
    client = LLMClient(app.settings.llm, app.stores, transport=httpx.MockTransport(handler), credentials=app.credentials)
    app.services.llm = app.knowledge.llm = app.memory.llm = client
    app.llm = client
    owner = app.guard.create_user("o@x.test", "O")
    demo.seed(app, owner)
    return app, owner


def test_hybrid_search_finds_meaning_without_shared_words(tmp_path, monkeypatch):
    app, owner = _app(tmp_path, monkeypatch)
    try:
        d = app.knowledge.add_document(owner, "northwind", b"Vendor terms\n\nThe vendor raised prices for Europe in July, which hurt profitability.",
                                       filename="vendor.md", title="Vendor terms")
        assert d["embedded"] is True
        app.knowledge.add_document(owner, "northwind", b"Office notes\n\nThe office kitchen is cleaned on Fridays and the plants are watered.",
                                   filename="office.md", title="Office notes")
        hits = app.knowledge.search(owner, "northwind", "supplier EMEA margin")  # no word in common with the vendor passage
        assert hits and hits[0]["title"] == "Vendor terms" and hits[0]["semantic"] is True
        assert all(h["title"] != "Office notes" for h in hits)
        calls = app.stores.control.query("SELECT workload, pool, outcome, model FROM llm_calls")
        assert calls and all(c["workload"] == "embed" and c["pool"] == "background" and c["outcome"] == "ok" for c in calls)
        assert calls[0]["model"] == "openai/text-embedding-3-small"
    finally:
        app.close()


def test_memory_recall_by_meaning(tmp_path, monkeypatch):
    app, owner = _app(tmp_path, monkeypatch)
    try:
        mid = app.memory.remember(owner, "northwind", "team", "Vendor price rises in Europe explain the profitability drop.",
                                  provenance={"kind": "correction"}, trust="user_validated")
        assert app.stores.workspace("northwind").scalar("SELECT count(*) FROM memory_vectors WHERE memory_id = ?", [mid]) == 1
        got = app.memory.recall(owner, "northwind", "why did supplier costs hurt EMEA margin")
        assert got and got[0]["id"] == mid
        assert not app.memory.recall(owner, "northwind", "office kitchen cleaning")
    finally:
        app.close()


def test_reindex_backfills_and_external_policy_applies(tmp_path, monkeypatch):
    app, owner = _app(tmp_path, monkeypatch)
    try:
        app.stores.control.execute("UPDATE workspaces SET allow_external_llm = false WHERE id = 'northwind'")
        d = app.knowledge.add_document(owner, "northwind", b"Sales plan\n\nTurnover targets for Europe rise next year.", filename="s.md")
        assert d["embedded"] is False  # external provider not allowed: keyword search only, nothing sent
        assert app.stores.control.scalar("SELECT count(*) FROM llm_calls") == 0
        app.stores.control.execute("UPDATE workspaces SET allow_external_llm = true WHERE id = 'northwind'")
        assert app.knowledge.reindex(owner, "northwind") == {"embedded": 1}
        assert app.knowledge.search(owner, "northwind", "revenue EMEA")[0]["semantic"] is True
    finally:
        app.close()
