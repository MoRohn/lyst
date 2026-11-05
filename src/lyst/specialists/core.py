# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""The Universal Analyst Core: the one entry point every analytical request goes through, whatever its channel.

Typed text, a voice transcript, an uploaded document, an API call and a scheduled run all arrive as an
``AnalystRequest`` and leave as an ``AnalystResponse``. The core performs the specialization part of
Understand → Contextualize → Classify → Assemble, then hands the request to Lyst's existing durable runtime, which
runs Plan → Execute → Verify → Synthesize → Deliver → Learn exactly as before, with the assembled expertise stored
on the task (``request["expertise"]``) and in its trace (a task event of kind ``expertise``).

What the core guarantees:
- **Same request, same expertise.** Routing depends only on the text, the person's stated preference, their
  permissions and the workspace profile, never on the channel. A spoken request and the same typed request select
  the same disciplines and run the same pipeline.
- **No privilege from a role.** Permissions are checked against the actor's own role; a discipline the actor cannot
  fully use contributes its perspective only, and the runtime's own permission checks still apply to every step.
- **Small context.** Only the selected profiles are assembled, into a brief of bounded size; the catalog is never
  sent to a model.
- **Honesty.** Missing capabilities and unsupported task classes come back as limitations, with the Foundry as the
  safe next step, instead of being silently attempted.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..security import Principal
from .assembly import assemble
from .catalog import load
from .router import route

Channel = Literal["text", "voice", "document", "spreadsheet", "image", "audio", "video", "api", "schedule"]
CONTRACT_VERSION = "1"
VOICE_MIN_CONFIDENCE = 0.6


class Attachment(BaseModel):
    kind: Literal["dataset", "document", "artifact", "file"]
    ref: str = Field(min_length=1, max_length=200)


class AnalystRequest(BaseModel):
    """What any channel hands to the core."""

    workspace_id: str
    text: str = Field(min_length=1, max_length=8000)
    channel: Channel = "text"
    prefer: list[str] = Field(default_factory=list, max_length=4)  # specialization ids the person asked for
    attachments: list[Attachment] = Field(default_factory=list, max_length=20)
    autonomy: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    submit: bool = True                       # False: route and explain only, no task
    transcript_confidence: float | None = Field(default=None, ge=0, le=1)  # voice: the recognizer's confidence

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("the request is empty")
        return v


class AnalystResponse(BaseModel):
    contract: str = CONTRACT_VERSION
    request_id: str
    status: Literal["queued", "routed", "needs_confirmation"]
    channel: Channel
    task_id: str | None = None
    message: str                       # one plain sentence the channel can show or speak
    expertise: dict[str, Any]          # the assembly, without the model-facing brief
    routing: dict[str, Any]            # the decision with reasons and what was considered
    limitations: list[str]


def _say(assembly: dict[str, Any]) -> str:
    if assembly.get("general"):
        return "Lyst will answer this as a general analyst; no specialized discipline was needed."
    names = [r["name"] for r in assembly["roles"]]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    text = f"Lyst will approach this with {who} expertise."
    if assembly["capabilities"]["missing"] or assembly["unsupported"]:
        text += " Part of it needs methods Lyst does not have yet; the answer will say which."
    return text


class UniversalAnalystCore:
    def __init__(self, lyst) -> None:
        self.lyst = lyst
        load()  # validate and cache the catalog once at start-up, not on the first request

    def _workspace_domains(self, actor: Principal, ws: str) -> list[str]:
        ob = getattr(self.lyst, "onboarding", None)
        if ob is None:
            return []
        try:
            return list(ob.profile(actor, ws)["spec"].get("domains") or [])
        except Exception:  # noqa: BLE001 - the profile only nudges routing; its absence must not fail a request
            return []

    def route(self, actor: Principal, req: AnalystRequest):
        guard = self.lyst.guard
        guard.require(actor, req.workspace_id, "analyst.ask")
        decision = route(req.text, can=lambda perm: guard.can(actor, req.workspace_id, perm), prefer=req.prefer,
                         workspace_domains=self._workspace_domains(actor, req.workspace_id))
        return decision, assemble(decision, req.text)

    def handle(self, actor: Principal, req: AnalystRequest) -> AnalystResponse:
        t0 = time.perf_counter()
        decision, assembly = self.route(actor, req)
        rid = "ar_" + uuid.uuid4().hex[:12]
        public = {k: v for k, v in assembly.items() if k != "brief"}
        limitations = list(assembly.get("limitations", []))
        if decision.unresolved_overrides:
            limitations.append("No specialization matched " + ", ".join(f'"{u}"' for u in decision.unresolved_overrides)
                               + "; Lyst chose the expertise itself.")
        status: str = "routed"
        task_id = None
        message = _say(assembly)
        if req.channel == "voice" and req.transcript_confidence is not None and req.transcript_confidence < VOICE_MIN_CONFIDENCE:
            status = "needs_confirmation"
            message = f'I heard: "{req.text}". Is that right? Nothing has started yet.'
        elif req.submit:
            extra = {"expertise": {k: assembly.get(k) for k in ("primary", "roles", "task_classes", "methods", "verification",
                                                              "approvals", "sensitivity", "planning_depth", "limitations",
                                                              "catalog_version", "general", "brief")},
                     "channel": req.channel, "analyst_request_id": rid,
                     "attachments": [a.model_dump() for a in req.attachments]}
            try:
                task_id = self.lyst.runtime.submit(actor, req.workspace_id, req.text, autonomy=req.autonomy,
                                                   idempotency_key=req.idempotency_key, extra=extra)
            except Exception as exc:
                self.lyst.guard.audit(actor, req.workspace_id, "analyst.request", rid, "error", {
                    "channel": req.channel, "error": type(exc).__name__})
                raise
            try:  # the trace event is informative; it must not fail a submission that was accepted
                self.lyst.runtime.event(task_id, "expertise", message, {
                    "selected": [r["id"] for r in assembly.get("roles", [])], "task_classes": decision.request_task_classes,
                    "channel": req.channel, "general": decision.general, "routing_ms": round(decision.latency_ms, 2),
                    "catalog_version": decision.catalog_version, "request_id": rid})
            except Exception:  # noqa: BLE001
                logging.getLogger("lyst.specialists").warning("expertise event for task %s was not recorded", task_id,
                                                              exc_info=True)
            status = "queued"
        self.lyst.guard.audit(actor, req.workspace_id, "analyst.request", task_id or rid, "ok", {
            "channel": req.channel, "selected": [r["id"] for r in assembly.get("roles", [])], "status": status,
            "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return AnalystResponse(request_id=rid, status=status, channel=req.channel, task_id=task_id, message=message,
                               expertise=public, routing=decision.as_dict(), limitations=limitations)

    def expertise_for_task(self, actor: Principal, task_id: str) -> dict[str, Any]:
        task = self.lyst.runtime.get(actor, task_id)  # checks the actor may read the task
        ex = (task.get("request") or {}).get("expertise")
        if not ex:  # one shape either way: every key is always present
            return {"task_id": task_id, "channel": None, "expertise": None,
                    "note": "This task was not routed through the Analyst Core."}
        return {"task_id": task_id, "channel": task["request"].get("channel"),
                "expertise": {k: v for k, v in ex.items() if k != "brief"}, "note": None}
