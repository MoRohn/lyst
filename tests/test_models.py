# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The model catalog and tier choice: OpenRouter's list → Acumane's tiers, prices from the catalog, the
workspace choice under the installation's ceiling, and what Settings shows."""

import httpx
import pytest
from fastapi.testclient import TestClient

from lyst import demo
from lyst.api import create_app
from lyst.llm import catalog, policy


def row(mid, prompt, completion, *, created=1_790_000_000, tools=True, ctx=200_000, name=None, **extra):
    return {"id": mid, "name": name or mid, "created": created, "context_length": ctx,
            "pricing": {"prompt": str(prompt), "completion": str(completion), "input_cache_read": str(prompt / 10)},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "response_format"] if tools else ["temperature"], **extra}


PAYLOAD = {"data": [
    row("anthropic/claude-haiku-9", 1e-7, 5e-7, name="Anthropic: Claude Haiku 9"),
    row("anthropic/claude-haiku-8", 5e-8, 2e-7, created=1_700_000_000),     # older: the newest of a line wins
    row("anthropic/claude-sonnet-9", 2e-6, 1e-5),
    row("anthropic/claude-opus-9", 4e-6, 2e-5),
    row("anthropic/claude-opus-9:batch", 2e-6, 1e-5),                     # variants are noise
    row("openai/gpt-9-mini", 7.5e-7, 4.5e-6),
    row("openai/gpt-9", 5e-6, 3e-5, created=1_790_000_100),
    row("openai/gpt-8", 2.5e-6, 1.5e-5),                                   # second plain GPT is the mid model
    row("google/gemini-9-flash-lite", 3e-7, 2.5e-6),
    row("google/gemini-9-flash", 8e-7, 4e-6),
    row("google/gemini-9-pro", 2e-6, 1.2e-5),
    row("x-ai/grok-9.20", 1.25e-6, 2.5e-6),                                # minor >= 10: value line, balanced
    row("x-ai/grok-9.7", 2e-6, 6e-6),
    row("deepseek/deepseek-v9-flash", 1.5e-7, 6e-7, expiration_date="2020-01-01"),  # expired: never placed
    row("mistralai/mistral-large-9-0", 1.4e-6, 4.2e-6, tools=False),        # no tools: not eligible
    row("openai/gpt-9-image", 1e-6, 1e-6),                                 # image line: noise
    row("acme/frontier-x", 3e-6, 1.5e-5),                                  # unknown vendor: pickable, never tiered
]}


def test_release_follows_acumane_policy():
    rel = catalog.build_release(PAYLOAD)
    assert catalog.validate(rel) == []
    ids = {t: [m["modelId"] for m in rel["tiers"][t]] for t in catalog.TIER_ORDER}
    assert ids["economy"] == ["anthropic/claude-haiku-9", "google/gemini-9-flash-lite", "openai/gpt-9-mini"]
    assert ids["balanced"] == ["x-ai/grok-9.20", "google/gemini-9-flash", "anthropic/claude-sonnet-9", "openai/gpt-8"]
    assert ids["frontier"] == ["x-ai/grok-9.7", "google/gemini-9-pro", "anthropic/claude-opus-9", "openai/gpt-9"]
    # each tier is ordered by a typical call (800 in, 400 out), cheapest first
    for t in catalog.TIER_ORDER:
        costs = [catalog.typical_call_usd(m) for m in rel["tiers"][t]]
        assert costs == sorted(costs)
    haiku = rel["tiers"]["economy"][0]
    assert haiku["modelLabel"] == "Claude Haiku 9"
    assert catalog.typical_call_usd(haiku) == pytest.approx(800 * 1e-7 + 400 * 5e-7)
    picker = {m["modelId"] for m in rel["models"]}
    assert "acme/frontier-x" in picker and "anthropic/claude-opus-9:batch" not in picker
    assert "mistralai/mistral-large-9-0" not in picker and "deepseek/deepseek-v9-flash" not in picker


def test_invalid_release_is_refused_and_previous_stays(lyst):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        assert "authorization" not in req.headers  # the model list is public; no key is sent
        return httpx.Response(200, json={"data": [row("anthropic/claude-haiku-9", 1e-7, 5e-7)]})

    cat = catalog.ModelCatalog(lyst.stores, base_url="https://openrouter.test/api/v1", http=httpx.Client(transport=httpx.MockTransport(handler)))
    before = catalog.active()
    with pytest.raises(ValueError, match="previous one stays"):
        cat.refresh()
    assert calls == ["https://openrouter.test/api/v1/models"]
    assert catalog.active() is before and cat.stored() is None


def test_refresh_stores_activates_and_reports_changes(lyst):
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=PAYLOAD)))
    cat = catalog.ModelCatalog(lyst.stores, base_url="https://openrouter.test/api/v1", http=http)
    out = cat.refresh(actor="u1")
    assert out["changes"] == ["first catalog from OpenRouter"] and out["selectable"] >= 12
    assert policy.attempts("frontier")[0] == "x-ai/grok-9.7"
    assert policy.estimate_cost("acme/frontier-x", 1000, 100) == pytest.approx(1000 * 3e-6 + 100 * 1.5e-5)
    # a new process reads the stored release
    catalog.activate(catalog.bundled())
    catalog.ModelCatalog(lyst.stores)
    assert catalog.active()["source"] == "openrouter" and not catalog.stale(catalog.active())
    # a price change is reported in plain words
    cheaper = {"data": [dict(r, pricing={**r["pricing"], "prompt": "1e-6"}) if r["id"] == "openai/gpt-9" else r for r in PAYLOAD["data"]]}
    cat.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=cheaper)))
    assert any(c.startswith("openai/gpt-9 price changed") for c in cat.refresh()["changes"])
    catalog.activate(catalog.bundled())


def test_selection_order_and_ceiling():
    catalog.activate(catalog.build_release(PAYLOAD))
    try:
        s = policy.resolve(default_tier="balanced")
        assert (s.tier, s.model, s.source, s.attempts[0]) == ("balanced", None, "default", "x-ai/grok-9.20")
        s = policy.resolve(installation=("frontier", None), workspace=("economy", None))
        assert (s.tier, s.source, len(s.attempts)) == ("economy", "workspace", 3)
        s = policy.resolve(workspace=(None, "anthropic/claude-opus-9"), installation=("balanced", None))
        assert s.attempts == ("anthropic/claude-opus-9",) and s.route_tier == "frontier" and s.mode == "pinned"
        assert policy.resolve(workspace=(None, "acme/frontier-x")).route_tier == "custom"
        # a ceiling of balanced refuses frontier and any model dearer than balanced's dearest
        assert "up to balanced" in policy.choice_allowed("frontier", None, "balanced")
        assert "costs more per call" in policy.choice_allowed(None, "openai/gpt-9", "balanced")
        assert policy.choice_allowed(None, "x-ai/grok-9.20", "balanced") is None
        assert "not in OpenRouter's catalog" in policy.choice_allowed(None, "nobody/unknown", "frontier")
        # a workspace choice above a later-lowered ceiling falls back to the installation's
        s = policy.resolve(workspace=("frontier", None), installation=("economy", None), ceiling="balanced")
        assert (s.tier, s.source) == ("economy", "installation")
    finally:
        catalog.activate(catalog.bundled())


def test_bundled_snapshot_is_a_valid_openrouter_release():
    b = catalog.bundled()
    assert b["source"] == "bundled" and catalog.validate(b) == [] and len(b["models"]) > 20
    assert catalog.stale(b)  # a bundled release is always refreshed when allowed


def test_api_choice_options_and_usage(seeded, monkeypatch):
    lyst, owner = seeded
    catalog.activate(catalog.build_release(PAYLOAD))
    viewer = lyst.guard.create_user("v@northwind.test", "V")
    lyst.guard.add_member(owner, "northwind", viewer.user_id, "viewer") if hasattr(lyst.guard, "add_member") else None
    h = {"Authorization": f"Bearer {lyst.guard.issue_token(owner)}"}
    try:
        with TestClient(create_app(lyst, start_workers=False)) as c:
            o = c.get("/v1/workspaces/northwind/model-options", headers=h).json()
            assert o["selection"]["tier"] == "balanced" and o["selection"]["source"] == "default"
            assert [t["id"] for t in o["tiers"]] == ["economy", "balanced", "frontier"]
            assert o["basis"] == {"observed": False, "calls": 0, "input_tokens": 800, "output_tokens": 400, "calls_per_question": None}
            eco = o["tiers"][0]
            assert eco["primary"]["modelId"] == "anthropic/claude-haiku-9"
            assert eco["primary"]["input_per_m"] == pytest.approx(0.1) and eco["primary"]["output_per_m"] == pytest.approx(0.5)
            assert eco["per_call_usd"] == pytest.approx(0.00028)
            # the workspace picks frontier; calls walk it and are recorded under it
            assert c.patch("/v1/workspaces/northwind/settings", json={"llm_tier": "frontier"}, headers=h).status_code == 200
            assert lyst.llm.models("northwind")[0] == "x-ai/grok-9.7"
            assert c.get("/v1/workspaces/northwind/settings", headers=h).json()["workspace"]["llm_tier"] == "frontier"
            # pin one model; "auto" goes back to walking the tier
            c.patch("/v1/workspaces/northwind/settings", json={"llm_model": "anthropic/claude-opus-9"}, headers=h)
            assert lyst.llm.models("northwind") == ["anthropic/claude-opus-9"]
            c.patch("/v1/workspaces/northwind/settings", json={"llm_model": "auto"}, headers=h)
            assert len(lyst.llm.models("northwind")) == 4
            # the installation lowers the ceiling: frontier is refused, and the workspace falls back
            assert c.patch("/v1/installation", json={"settings": {"LYST_LLM_MAX_TIER": "balanced"}}, headers=h).status_code == 200
            assert lyst.llm.selection("northwind").source == "default"
            r = c.patch("/v1/workspaces/northwind/settings", json={"llm_tier": "frontier"}, headers=h)
            assert r.status_code == 403 and "up to balanced" in r.text
            o = c.get("/v1/workspaces/northwind/model-options", headers=h).json()
            assert [t["allowed"] for t in o["tiers"]] == [True, True, False]
            assert not next(m for m in o["models"] if m["modelId"] == "openai/gpt-9")["allowed"]
            # usage carries the tier calls ran under
            lyst.stores.control.execute(
                "INSERT INTO llm_calls (id, workspace_id, model, outcome, input_tokens, output_tokens, cost_usd, tier, workload) "
                "VALUES (nextval('llm_call_seq'), 'northwind', 'x-ai/grok-9.7', 'ok', 900, 300, 0.0036, 'frontier', 'chat')")
            u = c.get("/v1/workspaces/northwind/model-usage", headers=h).json()
            assert u[0]["tier"] == "frontier" and u[0]["cost_usd"] == pytest.approx(0.0036)
            o = c.get("/v1/workspaces/northwind/model-options", headers=h).json()
            assert o["spend"]["cost_usd"] == pytest.approx(0.0036) and o["spend"]["by_tier"][0]["tier"] == "frontier"
            # refreshing the catalog is for installation administrators, and is audited
            monkeypatch.setattr(lyst.llm.catalog, "http", httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=PAYLOAD))))
            r = c.post("/v1/models/catalog/refresh", headers=h)
            assert r.status_code == 200 and r.json()["modelCount"] == len(PAYLOAD["data"])
            assert lyst.stores.control.scalar("SELECT count(*) FROM credential_events WHERE action = 'models.catalog_refresh'") == 1
    finally:
        catalog.activate(catalog.bundled())


def test_call_records_route_tier(tmp_path, monkeypatch):
    import json  # noqa: PLC0415

    from lyst.app import Lyst  # noqa: PLC0415
    from lyst.config import load_settings  # noqa: PLC0415
    from lyst.llm import LLMClient  # noqa: PLC0415

    monkeypatch.setenv("LYST_MASTER_KEY", "cd" * 32)
    monkeypatch.setenv("LYST_OPENROUTER_API_KEY", "sk-or-test")
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    app = Lyst.build(load_settings(tmp_path / "h"))
    app.services.llm = llm = LLMClient(app.settings.llm, app.stores, transport=httpx.MockTransport(handler), credentials=app.credentials)
    try:
        owner = app.guard.create_user("o@x.test", "O")
        demo.seed(app, owner)
        app.stores.control.execute("UPDATE workspaces SET llm_tier = 'economy' WHERE id = 'northwind'")
        llm.chat("northwind", [{"role": "user", "content": "hi"}], purpose="test")
        first = policy.tier_models("economy")[0]
        assert seen == [first["modelId"]]
        r = app.stores.control.query("SELECT tier, cost_usd FROM llm_calls")[0]
        # no usage.cost from the provider: the cost comes from the catalog's prices
        assert r["tier"] == "economy" and r["cost_usd"] == pytest.approx(catalog.call_usd(first, 10, 2))
    finally:
        app.close()


def test_migration_versions_are_unique():
    from lyst.store.schema import CONTROL, WORKSPACE  # noqa: PLC0415

    for migrations in (CONTROL, WORKSPACE):
        versions = [v for v, _ in migrations]
        assert len(versions) == len(set(versions)), sorted(versions)
