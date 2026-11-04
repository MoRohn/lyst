"""Provider-neutral inference over the OpenAI-compatible chat API.

Ollama, llama.cpp's server, vLLM and LiteLLM all speak this API, so one adapter covers local
and hosted models. Lyst runs fully without a model: rules plan the work and templates write
the answer. A model, when configured, only interprets requests and words the findings.

Data policy: an endpoint marked external receives nothing unless the workspace allows it, and
what it receives is redacted first. Every call is recorded in ``llm_calls``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import LLMSettings
from ..security import redact
from ..store import Stores


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class ModelInfo:
    name: str
    base_url: str
    external: bool
    capabilities: tuple[str, ...] = ("chat", "json")


class LLMClient:
    def __init__(self, settings: LLMSettings, stores: Stores, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.stores = stores
        self._http = httpx.Client(timeout=60, transport=transport) if settings.configured else None

    @property
    def available(self) -> bool:
        return self._http is not None

    def info(self) -> ModelInfo | None:
        if not self.settings.configured:
            return None
        return ModelInfo(self.settings.model or "", self.settings.base_url or "", self.settings.external)

    def allowed_for(self, workspace_id: str) -> bool:
        if not self.available:
            return False
        if not self.settings.external:
            return True
        return bool(self.stores.control.scalar(
            "SELECT allow_external_llm FROM workspaces WHERE id = ?", [workspace_id]))

    def chat(self, workspace_id: str, messages: list[dict[str, str]], *, purpose: str,
             task_id: str | None = None, json_mode: bool = False, max_tokens: int = 800) -> str:
        if not self.allowed_for(workspace_id):
            raise LLMUnavailable("no model is allowed for this workspace")
        if self.settings.external:
            messages = [{**m, "content": redact(m["content"])} for m in messages]
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.settings.api_key}"} if self.settings.api_key else {}
        start = time.perf_counter()
        outcome, usage, text = "ok", {}, ""
        try:
            resp = self._http.post(self.settings.base_url.rstrip("/") + "/chat/completions",
                                   json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
            return text
        except Exception as exc:
            outcome = f"error:{type(exc).__name__}"
            raise LLMUnavailable(str(exc)) from exc
        finally:
            self.stores.control.execute(
                "INSERT INTO llm_calls (id, workspace_id, task_id, model, purpose, external, input_tokens, "
                "output_tokens, latency_ms, outcome) VALUES (nextval('llm_call_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [workspace_id, task_id, self.settings.model, purpose, self.settings.external,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 int((time.perf_counter() - start) * 1000), outcome],
            )

    def chat_json(self, workspace_id: str, messages: list[dict[str, str]], **kw) -> dict[str, Any]:
        text = self.chat(workspace_id, messages, json_mode=True, **kw)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise LLMUnavailable("model did not return JSON")
        return json.loads(text[start : end + 1])
