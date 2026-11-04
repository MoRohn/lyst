# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""What Settings › Inference shows: each tier and model with its OpenRouter price, what a call and a question
cost on it, which of them this workspace may choose, and what it spent. Every price comes from the catalog
(OpenRouter's published per-token prices); every estimate says which token counts it assumed."""

from __future__ import annotations

from . import catalog, policy

MIN_OBSERVED_CALLS = 20


def _per_m(usd_per_token: float) -> float:
    return round(usd_per_token * 1_000_000, 6)


def _row(m: dict, basis: tuple[int, int], calls_per_question: float | None, allowed: str | None) -> dict:
    per_call = catalog.call_usd(m, *basis)
    return {"modelId": m["modelId"], "modelLabel": m["modelLabel"], "vendor": m.get("vendor"),
            "vendorLabel": catalog.VENDOR_LABEL.get(m.get("vendor") or "", (m["modelId"].split("/")[0]).capitalize()),
            "context": m["context"], "input_per_m": _per_m(m["inputUsdPerToken"]), "output_per_m": _per_m(m["outputUsdPerToken"]),
            "cached_per_m": _per_m(m.get("cachedInputUsdPerToken", 0.0)), "per_request_usd": m.get("perRequestUsd", 0.0),
            "typical_call_usd": catalog.typical_call_usd(m), "per_call_usd": per_call,
            "per_question_usd": per_call * calls_per_question if calls_per_question else None,
            "reasoning": m.get("reasoning"), "vision": m.get("vision"), "createdAt": m.get("createdAt"),
            "expiresOn": m.get("expiresOn"), "tier": catalog.tier_of_model(m["modelId"]),
            "allowed": allowed is None, "reason": allowed}


def basis(stores, ws: str) -> dict:
    """Token counts the estimates use: this workspace's own average over 30 days once it has enough calls,
    else Acumane's typical call (800 in, 400 out)."""
    r = stores.control.query(
        "SELECT count(*) AS calls, avg(input_tokens) AS i, avg(output_tokens) AS o, "
        "count(DISTINCT task_id) FILTER (WHERE task_id IS NOT NULL) AS tasks, "
        "count(*) FILTER (WHERE task_id IS NOT NULL) AS task_calls "
        "FROM llm_calls WHERE workspace_id = ? AND outcome = 'ok' AND input_tokens IS NOT NULL "
        "AND coalesce(workload, '') <> 'embed' AND occurred_at > now() - INTERVAL 30 DAY", [ws])[0]
    observed = (r["calls"] or 0) >= MIN_OBSERVED_CALLS
    per_q = round(r["task_calls"] / r["tasks"], 2) if r["tasks"] else None
    return {"observed": observed, "calls": r["calls"] or 0,
            "input_tokens": int(round(r["i"])) if observed else catalog.ESTIMATE_INPUT_TOKENS,
            "output_tokens": int(round(r["o"] or 0)) if observed else catalog.ESTIMATE_OUTPUT_TOKENS,
            "calls_per_question": per_q if observed else None}


def spend(stores, ws: str) -> dict:
    rows = stores.control.query(
        "SELECT coalesce(tier, 'unrecorded') AS tier, count(*) AS calls, round(coalesce(sum(cost_usd), 0), 6) AS cost_usd, "
        "coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens "
        "FROM llm_calls WHERE workspace_id = ? AND occurred_at > now() - INTERVAL 30 DAY GROUP BY ALL ORDER BY cost_usd DESC", [ws])
    return {"days": 30, "cost_usd": round(sum(r["cost_usd"] for r in rows), 6), "calls": sum(r["calls"] for r in rows),
            "by_tier": rows}


def model_options(llm, stores, ws: str) -> dict:
    sel = llm.selection(ws)
    if sel is None:
        info = llm.info(ws)
        return {"provider": llm.settings.provider or "local", "selection": None, "tiers": [], "models": [],
                "local_model": info.name if info else None, "spend": spend(stores, ws)}
    rel = catalog.active()
    b = basis(stores, ws)
    tokens = (b["input_tokens"], b["output_tokens"])
    tiers = []
    for t in catalog.TIER_ORDER:
        reason = policy.choice_allowed(t, None, sel.ceiling)
        rows = [_row(m, tokens, b["calls_per_question"], reason) for m in rel["tiers"][t]]
        costs = [r["per_call_usd"] for r in rows]
        tiers.append({"id": t, "label": catalog.TIER_LABEL[t], "purpose": catalog.TIER_PURPOSE[t], "allowed": reason is None,
                      "reason": reason, "primary": rows[0] if rows else None, "models": rows,
                      "per_call_usd": costs[0] if costs else None, "range_usd": [min(costs), max(costs)] if costs else None})
    picker = [_row(m, tokens, b["calls_per_question"], policy.choice_allowed(None, m["modelId"], sel.ceiling))
              for m in rel.get("models") or []]
    pinned = catalog.find(sel.model) if sel.model else None
    return {"provider": "openrouter",
            "catalog": {"source": rel.get("source"), "lastRefreshedAt": rel.get("lastRefreshedAt"), "policyId": rel.get("policyId"),
                        "modelCount": rel.get("modelCount"), "selectable": len(rel.get("models") or []),
                        "changes": rel.get("changes") or [], "stale": catalog.stale(rel)},
            "selection": {"tier": sel.tier, "model": sel.model, "mode": sel.mode, "source": sel.source, "ceiling": sel.ceiling,
                          "attempts": list(sel.attempts), "route_tier": sel.route_tier,
                          "model_label": pinned["modelLabel"] if pinned else None,
                          "per_call_usd": catalog.call_usd(pinned or catalog.find(sel.attempts[0]) or rel["tiers"][sel.tier][0], *tokens)},
            "basis": b, "tiers": tiers, "models": picker, "spend": spend(stores, ws)}
