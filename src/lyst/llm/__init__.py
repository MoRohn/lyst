# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Inference on Acumane's rails: OpenRouter through the gateway, or a local OpenAI-compatible model.

OpenRouter is the main provider when ``LYST_OPENROUTER_API_KEY`` is set: a call walks the tier's
model list (economy, balanced, frontier, copied from Acumane's published catalog) or one pinned
slug, through the admission, adaptive limits, retries and workload routing in ``gateway.py``. A
local endpoint (Ollama, llama.cpp, vLLM) uses the same gateway with one model. With neither, Lyst
runs on rules and templates.

Data policy: OpenRouter is an external provider. A workspace uses it only while its
``allow_external_llm`` is on (new workspaces take ``LYST_EXTERNAL_LLM_DEFAULT``, on by default with
OpenRouter), text is redacted before it leaves, and the analyst sends definitions and computed
findings, never dataset rows. Every upstream request is a row in ``llm_calls``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import httpx

from .. import observability as obs
from ..config import LLMSettings
from ..security import redact
from ..store import Stores
from . import policy
from .gateway import CallRecord, Gateway, GatewayError


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class ModelInfo:
    name: str
    base_url: str
    external: bool
    provider: str = "local"
    tier: str | None = None
    attempts: tuple[str, ...] = ()


class LLMClient:
    def __init__(self, settings: LLMSettings, stores: Stores, *, transport: httpx.BaseTransport | None = None,
                 gateway: Gateway | None = None, credentials=None):
        self.settings = settings
        self.stores = stores
        self.gateway = gateway
        self.credentials = credentials  # the broker; None only in unit tests that pass a static key
        self._local = threading.local()
        if self.gateway is None and settings.configured:
            self.gateway = Gateway(provider=settings.provider or "local", base_url=settings.base_url or "",
                                   api_key=self._key_for, http=httpx.Client(transport=transport), on_call=self._record)

    # -- keys come from the credential broker at call time, so a key connected in Settings works without a restart
    def _owner_of(self, task_id: str | None) -> str | None:
        """The user a task runs for, so a personal credential (where policy allows one) applies to their own work."""
        if not task_id:
            return None
        cache = getattr(self, "_owners", None)
        if cache is None:
            cache = self._owners = {}
        if task_id not in cache:
            cache[task_id] = self.stores.control.scalar("SELECT created_by FROM tasks WHERE id = ?", [task_id])
            if len(cache) > 512:
                cache.pop(next(iter(cache)))
        return cache[task_id]

    def _current_user(self) -> str | None:
        return getattr(self._local, "user_id", None) or obs.user_id.get()

    def _key_for(self, tenant: str | None = None) -> str | None:
        ws = None if tenant in (None, "platform") else tenant
        if self.credentials is None:
            return self.settings.api_key
        provider = "openrouter" if self.settings.provider == "openrouter" else "local_llm"
        got = self.credentials.use(provider, purpose="inference", workspace=ws, user=self._current_user(), consumer="gateway")
        if got:
            return got[0]
        return None if provider == "openrouter" else self.settings.api_key

    def _setting(self, key: str, default: str | None = None) -> str | None:
        import os  # noqa: PLC0415

        v = self.credentials.setting(key) if self.credentials is not None else None
        return v or os.environ.get(key) or default

    def available_for(self, workspace_id: str | None = None) -> bool:
        if self.gateway is None:
            return False
        if self.settings.provider == "openrouter":
            if self.credentials is None:
                return bool(self.settings.api_key)
            return self.credentials.resolve("openrouter", workspace=workspace_id, user=self._current_user()) is not None
        return True

    @property
    def available(self) -> bool:
        return self.available_for(None)

    def models(self) -> list[str]:
        if self.settings.provider == "openrouter":
            return policy.attempts(self._setting("LYST_LLM_TIER", self.settings.tier), self._setting("LYST_LLM_MODEL", self.settings.model))
        return [self.settings.model] if self.settings.model else []

    def info(self, workspace_id: str | None = None) -> ModelInfo | None:
        if not self.available_for(workspace_id):
            return None
        models = self.models()
        pinned = self._setting("LYST_LLM_MODEL", self.settings.model)
        return ModelInfo(models[0], self.settings.base_url or "", self.settings.external,
                         self.settings.provider or "local",
                         policy.tier_of(self._setting("LYST_LLM_TIER", self.settings.tier)) if not pinned else None, tuple(models))

    def allowed_for(self, workspace_id: str) -> bool:
        if not self.available_for(workspace_id):
            return False
        if not self.settings.external:
            return True
        return bool(self.stores.control.scalar(
            "SELECT allow_external_llm FROM workspaces WHERE id = ?", [workspace_id]))

    def _record(self, r: CallRecord) -> None:
        cost = r.cost_usd if r.cost_usd is not None else policy.estimate_cost(r.model, r.input_tokens, r.output_tokens)
        obs.MODEL_CALLS.labels(r.outcome, r.pool).inc()
        if cost:
            obs.MODEL_COST.inc(cost)
        self.stores.control.execute(
            "INSERT INTO llm_calls (id, workspace_id, task_id, model, purpose, external, input_tokens, output_tokens, "
            "latency_ms, outcome, pool, workload, provider, status, attempt, queue_ms, cost_usd) "
            "VALUES (nextval('llm_call_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [r.tenant, getattr(self._local, "task_id", None), r.model, getattr(self._local, "purpose", None), self.settings.external,
             r.input_tokens, r.output_tokens, r.latency_ms, r.outcome, r.pool, r.workload, r.provider, r.status,
             r.attempt, r.queue_ms, cost],
        )

    def chat(self, workspace_id: str, messages: list[dict[str, str]], *, purpose: str, task_id: str | None = None,
             json_mode: bool = False, max_tokens: int | None = None, workload: str = "chat",
             pool: str | None = None) -> str:
        self._local.task_id, self._local.purpose = task_id, purpose  # read by _record on this thread
        self._local.user_id = self._owner_of(task_id)
        if not self.allowed_for(workspace_id):
            raise LLMUnavailable("no model is allowed for this workspace")
        if self.settings.external:
            messages = [{**m, "content": redact(m["content"])} for m in messages]
        try:
            with obs.tracer().start_as_current_span("model.call") as span:
                span.set_attribute("lyst.workload", workload)
                span.set_attribute("lyst.purpose", purpose)
                r = self.gateway.complete(messages, models=self.models(), workload=workload, tenant=workspace_id,
                                          pool=pool, json_mode=json_mode, max_tokens=max_tokens)
                span.set_attribute("lyst.model", r.model)
                return r.text
        except GatewayError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

    @property
    def embed_model(self) -> str | None:
        return self._setting("LYST_EMBED_MODEL")

    def can_embed(self, workspace_id: str) -> bool:
        return bool(self.embed_model) and self.allowed_for(workspace_id)

    def embed(self, workspace_id: str, texts: list[str], *, purpose: str = "embed") -> list[list[float]]:
        if not self.can_embed(workspace_id):
            raise LLMUnavailable("no embedding model is allowed for this workspace")
        if self.settings.external:
            texts = [redact(t) for t in texts]
        self._local.task_id, self._local.purpose = None, purpose
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            try:
                vectors, _ = self.gateway.embed(texts[i:i + 64], model=self.embed_model, tenant=workspace_id)
            except GatewayError as exc:
                raise LLMUnavailable(str(exc)) from exc
            out.extend(vectors)
        return out

    def chat_json(self, workspace_id: str, messages: list[dict[str, str]], **kw) -> dict:
        text = self.chat(workspace_id, messages, json_mode=True, **kw)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise LLMUnavailable("model did not return JSON")
        try:
            return json.loads(text[start: end + 1])
        except ValueError as exc:
            raise LLMUnavailable(f"model returned malformed JSON: {str(exc)[:80]}") from exc

    def status(self) -> dict:
        if not self.gateway:
            return {"configured": False}
        return {"configured": True, **self.gateway.admission.snapshot()}
