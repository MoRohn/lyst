# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The model catalog: OpenRouter's live model list, its prices, and the three quality tiers built from it.

Ported from Acumane's ``packages/config/src/catalog-policy.ts`` (policy ``latest-flagship-2026-10``) and
``tier-rates.ts``. Acumane rebuilds its tiers every week from ``GET /api/v1/models``; Lyst does the same
on demand (Settings, ``lyst models refresh``) and when the stored copy is older than a day:

* every model is normalised (prices per token as OpenRouter publishes them, context, tool support);
* a model is *eligible* for a tier when it supports tools, is not deprecated or expiring, has a sane
  price and at least 8,000 tokens of context;
* each vendor's line (Claude Haiku/Sonnet/Opus, GPT mini/plain, Gemini Flash Lite/Flash/Pro, Grok,
  DeepSeek Flash/Pro, Mistral Small/Large, Llama Maverick) has a role (fast, mid, flagship), and the
  placement table says which role of which vendor goes in which tier; the newest model of that role wins;
* a tier lists at most one model per vendor, cheapest *typical call* first (800 input and 400 output
  tokens, Acumane's estimate), larger context breaking ties. Auto routing uses the first and falls back
  down the list.

A release that fails validation is never stored; the last good one (or the bundled ``tiers.json``)
stays in use. Prices shown anywhere in Lyst come from this catalog, never from constants in code.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

POLICY_ID = "latest-flagship-2026-10"
TIER_ORDER = ("economy", "balanced", "frontier")
TIER_LABEL = {"economy": "Economy", "balanced": "Balanced", "frontier": "Frontier"}
TIER_PURPOSE = {
    "economy": "Fast, low-cost models for routine questions and high volume.",
    "balanced": "Strong mid-size models; the default for analysis.",
    "frontier": "Each provider's flagship, for the hardest reasoning.",
}
ESTIMATE_INPUT_TOKENS = 800
ESTIMATE_OUTPUT_TOKENS = 400
STALE_AFTER = timedelta(hours=24)

VENDORS = ("anthropic", "openai", "google", "xai", "meta", "mistral", "deepseek")
VENDOR_LABEL = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "xai": "xAI", "meta": "Meta",
                "mistral": "Mistral", "deepseek": "DeepSeek"}
AUTHOR = {"anthropic": "anthropic", "openai": "openai", "google": "google", "x-ai": "xai", "meta-llama": "meta",
          "mistralai": "mistral", "deepseek": "deepseek"}

# Acumane's catalog-policy.json
POLICY = {
    "minUsdPerToken": 1e-9, "maxInputUsdPerToken": 1e-4, "maxOutputUsdPerToken": 2e-4, "minContext": 8000,
    "requireTools": True,
    "placement": {
        "anthropic": {"economy": "fast", "balanced": "mid", "frontier": "flagship"},
        "openai": {"economy": "fast", "balanced": "mid", "frontier": "flagship"},
        "google": {"economy": "fast", "balanced": "mid", "frontier": "flagship"},
        "xai": {"balanced": "fast", "frontier": "flagship"},
        "mistral": {"economy": "fast", "balanced": "flagship"},
        "deepseek": {"economy": "fast", "balanced": "flagship"},
        "meta": {"economy": "flagship"},
    },
}

_NOISE = re.compile(r"embed|image|audio|tts|whisper|moderation|realtime|transcribe|\bsearch\b|codex|nano|gemma|"
                    r"vision-exp|customtools|multi-agent|voxtral|codestral|devstral|ministral|mixtral")


def vendor_of(model_id: str) -> str | None:
    return AUTHOR.get(model_id.split("/")[0])


def is_noise(model_id: str) -> bool:
    v = model_id.lower()
    return v.startswith("~") or ":" in v or bool(_NOISE.search(v))


def _num(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n and n > 0 else 0.0  # NaN and negatives (OpenRouter's -1 "variable") count as 0


def _version(raw: str) -> list[int]:
    return [int(p) for p in re.split(r"[.-]", raw) if p.isdigit()]


def _label(name: str, model_id: str) -> str:
    name = re.sub(r"^(Anthropic|OpenAI|Google|Meta|Mistral|DeepSeek|SpaceXAI|xAI|Together|Groq):\s*", "", (name or "").strip(),
                  flags=re.I)
    return re.sub(r"\s+", " ", name).strip() or model_id


def family_of(model_id: str) -> tuple[str, str, list[int], int] | None:
    """(family, role, version, date stamp), the same rules as Acumane's familyOf."""
    if is_noise(model_id):
        return None
    if m := re.fullmatch(r"anthropic/claude-(haiku|sonnet|opus)-(\d+(?:\.\d+)?)", model_id):
        role = {"haiku": "fast", "sonnet": "mid"}.get(m[1], "flagship")
        return f"claude-{m[1]}", role, _version(m[2]), 0
    if m := re.fullmatch(r"openai/gpt-(\d+(?:\.\d+)?)-mini", model_id):
        return "gpt-mini", "fast", _version(m[1]), 0
    if m := re.fullmatch(r"openai/gpt-(\d+(?:\.\d+)?)", model_id):
        return "gpt", "flagship", _version(m[1]), 0
    if m := re.fullmatch(r"google/gemini-(\d+(?:\.\d+)?)-flash-lite", model_id):
        return "gemini-flash-lite", "fast", _version(m[1]), 0
    if m := re.fullmatch(r"google/gemini-(\d+(?:\.\d+)?)-flash", model_id):
        return "gemini-flash", "mid", _version(m[1]), 0
    if m := re.fullmatch(r"google/gemini-(\d+(?:\.\d+)?)-pro(?:-preview)?", model_id):
        return "gemini-pro", "flagship", _version(m[1]), 0
    if m := re.fullmatch(r"x-ai/grok-(\d+)\.(\d+)", model_id):
        value_line = int(m[2]) >= 10
        return ("grok-value" if value_line else "grok-flagship"), ("fast" if value_line else "flagship"), [int(m[1]), int(m[2])], 0
    if m := re.fullmatch(r"deepseek/deepseek-v(\d+(?:\.\d+)?)-flash(?:-(\d{4}))?", model_id):
        return "deepseek-flash", "fast", _version(m[1]), int(m[2] or 0)
    if m := re.fullmatch(r"deepseek/deepseek-v(\d+(?:\.\d+)?)-pro(?:-(\d{4}))?", model_id):
        return "deepseek-pro", "flagship", _version(m[1]), int(m[2] or 0)
    if m := re.fullmatch(r"mistralai/mistral-small-(\d{4})", model_id):
        return "mistral-small", "fast", [int(m[1])], int(m[1])
    if m := re.fullmatch(r"mistralai/mistral-large-(\d+)-(\d+)", model_id):
        return "mistral-large", "flagship", [int(m[1]), int(m[2])], 0
    if m := re.fullmatch(r"meta-llama/llama-(\d+)-maverick", model_id):
        return "llama-maverick", "flagship", [int(m[1])], 0
    return None


def typical_call_usd(m: dict) -> float:
    """Provider cost of Acumane's typical call: 800 input and 400 output tokens, plus any per-request price."""
    return (m["inputUsdPerToken"] * ESTIMATE_INPUT_TOKENS + m["outputUsdPerToken"] * ESTIMATE_OUTPUT_TOKENS
            + m.get("perRequestUsd", 0.0))


def call_usd(m: dict, input_tokens: int | None, output_tokens: int | None) -> float:
    return ((input_tokens or 0) * m["inputUsdPerToken"] + (output_tokens or 0) * m["outputUsdPerToken"]
            + m.get("perRequestUsd", 0.0))


def _created(value: Any) -> str | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value if value < 1e10 else value / 1000, UTC).isoformat()
    return None


def _reasoning_info(r: Any) -> dict | None:
    """OpenRouter's per-model ``reasoning`` block: whether thinking is mandatory, on by default, and which efforts exist."""
    if not isinstance(r, dict):
        return None
    efforts = [e for e in r.get("supported_efforts") or [] if isinstance(e, str)]
    default_on = r["default_enabled"] if isinstance(r.get("default_enabled"), bool) else r.get("default_effort") not in (None, "none")
    return {"mandatory": r.get("mandatory") is True, "defaultOn": bool(default_on), "efforts": efforts}


def normalize(payload: Any, *, now: datetime | None = None) -> list[dict]:
    """OpenRouter ``/models`` rows → Lyst's model rows (one per id), with vendor, family and role."""
    rows = payload.get("data", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    today = (now or datetime.now(UTC)).date().isoformat()
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            continue
        mid = row["id"].strip()
        pricing = row.get("pricing") or {}
        arch = row.get("architecture") or {}
        params = [p for p in row.get("supported_parameters") or [] if isinstance(p, str)]
        out_mod = [p for p in arch.get("output_modalities") or [] if isinstance(p, str)]
        expires = row.get("expiration_date") if isinstance(row.get("expiration_date"), str) else None
        hit = family_of(mid)
        model = {
            "modelId": mid, "modelLabel": _label(row.get("name") or "", mid), "vendor": vendor_of(mid),
            "context": int(_num(row.get("context_length"))),
            "inputUsdPerToken": _num(pricing.get("prompt")), "outputUsdPerToken": _num(pricing.get("completion")),
            "cachedInputUsdPerToken": _num(pricing.get("input_cache_read")), "perRequestUsd": _num(pricing.get("request")),
            "tools": "tools" in params, "structured": "structured_outputs" in params or "response_format" in params,
            "reasoning": "reasoning" in params, "reasoningInfo": _reasoning_info(row.get("reasoning")), "vision": "image" in (arch.get("input_modalities") or []),
            "text": not out_mod or "text" in out_mod,
            "createdAt": _created(row.get("created")), "expiresOn": expires,
            "deprecated": row.get("deprecated") is True or bool(expires and expires[:10] <= today),
            "family": hit[0] if hit else None, "role": hit[1] if hit else "skip",
            "version": hit[2] if hit else [], "stamp": hit[3] if hit else 0,
        }
        cur = out.get(mid)
        if cur is None or (model["tools"], model["context"]) > (cur["tools"], cur["context"]):
            out[mid] = model
    models = list(out.values())
    # the highest plain GPT is the flagship and the next one the mid model (Acumane's assignLineRoles)
    plains = sorted((m for m in models if m["family"] == "gpt"), key=lambda m: m["version"], reverse=True)
    for i, m in enumerate(plains):
        m["role"] = "flagship" if i == 0 else "mid" if i == 1 else "skip"
    return models


def price_is_sane(m: dict) -> bool:
    p = POLICY
    inp, outp, cached = m["inputUsdPerToken"], m["outputUsdPerToken"], m.get("cachedInputUsdPerToken", 0.0)
    if not (p["minUsdPerToken"] <= inp <= p["maxInputUsdPerToken"]):
        return False
    if not (p["minUsdPerToken"] <= outp <= p["maxOutputUsdPerToken"]):
        return False
    return 0 <= cached <= inp and m["context"] >= p["minContext"]


def selectable(m: dict) -> bool:
    """A text model Lyst can run: tools (the analyst calls functions), a sane price, not deprecated."""
    return bool(m.get("text", True) and m["tools"] and not m["deprecated"] and not is_noise(m["modelId"])
                and price_is_sane(m))


def _newest_key(m: dict) -> tuple:
    return (m.get("createdAt") or "", m["version"], m["stamp"], m["modelId"])


TIER_FIELDS = ("modelId", "modelLabel", "vendor", "context", "inputUsdPerToken", "outputUsdPerToken",
               "cachedInputUsdPerToken", "perRequestUsd", "reasoningInfo")
SCHEMA = 2  # 2: models carry reasoningInfo; a stored release of an older schema is refreshed


def select_tiers(models: list[dict]) -> dict[str, list[dict]]:
    pool = [m for m in models if m["vendor"] and selectable(m)]
    tiers: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for tier in TIER_ORDER:
        for vendor in VENDORS:
            role = POLICY["placement"].get(vendor, {}).get(tier)
            if not role:
                continue
            candidates = [m for m in pool if m["vendor"] == vendor and m["role"] == role]
            if candidates:
                chosen = max(candidates, key=_newest_key)
                tiers[tier].append({k: chosen.get(k) if k == "reasoningInfo" else chosen.get(k, 0.0) for k in TIER_FIELDS})
        tiers[tier].sort(key=lambda m: (typical_call_usd(m), -m["context"]))
    return tiers


def validate(release: dict) -> list[str]:
    errors = []
    for tier in TIER_ORDER:
        models = release.get("tiers", {}).get(tier) or []
        if not models:
            errors.append(f"{tier} has no models")
            continue
        vendors = [m["vendor"] for m in models]
        if len(set(vendors)) != len(vendors):
            errors.append(f"{tier} lists a provider more than once")
        errors += [f"{m['modelId']} has a price or context outside the policy" for m in models if not price_is_sane(m)]
    if not errors:
        primaries = [typical_call_usd(release["tiers"][t][0]) for t in TIER_ORDER]
        if not primaries[0] < primaries[1] < primaries[2]:
            errors.append("tier primaries are not ordered economy < balanced < frontier")
    return errors


def build_release(payload: Any, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    models = normalize(payload, now=now)
    picker = sorted((m for m in models if selectable(m)), key=lambda m: m.get("createdAt") or "", reverse=True)
    picker.sort(key=lambda m: (m["vendor"] is None, VENDOR_LABEL.get(m["vendor"] or "", m["modelId"].split("/")[0])))
    return {"source": "openrouter", "schema": SCHEMA, "policyId": POLICY_ID, "lastRefreshedAt": now.isoformat(),
            "modelCount": len(models), "tiers": select_tiers(models),
            "models": [{k: m[k] for k in (*TIER_FIELDS, "structured", "reasoning", "vision", "createdAt", "expiresOn", "family", "role")}
                       for m in picker]}


def diff(old: dict | None, new: dict) -> list[str]:
    """Plain-language changes between two releases' tiers, for the audit log and Settings."""
    if not old:
        return ["first catalog from OpenRouter"]
    lines = []
    for tier in TIER_ORDER:
        before = {m["modelId"]: m for m in old.get("tiers", {}).get(tier, [])}
        after = {m["modelId"]: m for m in new["tiers"][tier]}
        for mid in after.keys() - before.keys():
            lines.append(f"{TIER_LABEL[tier]} added {after[mid]['modelLabel']}")
        for mid in before.keys() - after.keys():
            lines.append(f"{TIER_LABEL[tier]} retired {before[mid]['modelLabel']}")
        for mid in after.keys() & before.keys():
            a, b = typical_call_usd(after[mid]), typical_call_usd(before[mid])
            if b and abs(a - b) / b > 0.01:
                lines.append(f"{after[mid]['modelLabel']} price changed {b * 1000:.3f} → {a * 1000:.3f} USD per 1,000 typical calls")
        if before and after and next(iter(before)) != next(iter(after)):
            lines.append(f"{TIER_LABEL[tier]} primary is now {new['tiers'][tier][0]['modelLabel']}")
    return lines


# ------------------------------------------------------------------------------------- active release

_lock = threading.Lock()
_active: dict | None = None


@cache
def bundled() -> dict:
    """The snapshot shipped with Lyst (an OpenRouter release built by this module), used until a refresh succeeds."""
    data = json.loads((Path(__file__).parent / "tiers.json").read_text())
    data["source"] = "bundled"
    return data


def active() -> dict:
    """The release calls route by: the last stored OpenRouter release, else the bundled snapshot."""
    global _active
    with _lock:
        if _active is None:
            _active = bundled()
        return _active


def activate(release: dict) -> None:
    global _active
    with _lock:
        _active = release


def find(model_id: str, release: dict | None = None) -> dict | None:
    rel = release or active()
    for m in rel.get("models") or []:
        if m["modelId"] == model_id:
            return m
    for models in rel["tiers"].values():
        for m in models:
            if m["modelId"] == model_id:
                return m
    return None


def tier_of_model(model_id: str, release: dict | None = None) -> str | None:
    rel = release or active()
    for tier in TIER_ORDER:
        if any(m["modelId"] == model_id for m in rel["tiers"][tier]):
            return tier
    return None


def stale(release: dict, now: datetime | None = None) -> bool:
    if release.get("source") != "openrouter" or release.get("schema", 1) < SCHEMA:
        return True
    try:
        at = datetime.fromisoformat(release["lastRefreshedAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return True
    return (now or datetime.now(UTC)) - at > STALE_AFTER


class ModelCatalog:
    """Keeps the release in the control database (``model_catalog``) and refreshes it from OpenRouter."""

    def __init__(self, stores, *, base_url: str = "https://openrouter.ai/api/v1", http=None):
        self.stores = stores
        self.base_url = base_url.rstrip("/")
        self.http = http
        self._refreshing = threading.Lock()
        activate(self.stored() or bundled())

    def stored(self) -> dict | None:
        raw = self.stores.control.scalar("SELECT payload FROM model_catalog WHERE id = 'openrouter'")
        if not raw:
            return None
        try:
            rel = json.loads(raw)
        except ValueError:
            return None
        return rel if not validate(rel) else None

    def current(self) -> dict:
        return active()

    def refresh(self, *, actor: str | None = None) -> dict:
        """Fetch OpenRouter's model list (public; no key is sent), rebuild the tiers, validate, store, activate."""
        import httpx  # noqa: PLC0415

        with self._refreshing:
            http = self.http or httpx.Client(timeout=30)
            resp = http.get(f"{self.base_url}/models", headers={"HTTP-Referer": "https://github.com/MoRohn/lyst", "X-Title": "Lyst"})
            resp.raise_for_status()
            release = build_release(resp.json())
            errors = validate(release)
            if errors:
                raise ValueError("OpenRouter's catalog did not pass validation; the previous one stays in use: " + "; ".join(errors))
            previous = self.stored()
            changes = diff(previous, release)
            release["changes"] = changes
            self.stores.control.execute(
                "INSERT OR REPLACE INTO model_catalog (id, fetched_at, fetched_by, payload) VALUES ('openrouter', now(), ?, ?)",
                [actor, json.dumps(release)])
            activate(release)
            return {"lastRefreshedAt": release["lastRefreshedAt"], "modelCount": release["modelCount"],
                    "selectable": len(release["models"]), "changes": changes}

    def refresh_if_stale(self) -> bool:
        """Start a background refresh when the release is over a day old; never blocks the caller.
        ``LYST_CATALOG_AUTO=0`` turns this off (air-gapped installations refresh by hand or not at all)."""
        import os  # noqa: PLC0415

        if os.environ.get("LYST_CATALOG_AUTO") == "0" or not stale(active()) or self._refreshing.locked():
            return False

        def run():
            try:
                self.refresh(actor="lyst")
            except Exception as exc:  # noqa: BLE001 - a failed background refresh keeps the last good release
                logging.getLogger("lyst.models").warning("model catalog refresh failed; keeping the last good one: %s", exc)

        threading.Thread(target=run, name="lyst-model-catalog", daemon=True).start()
        return True
