# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Routes of the extensibility lane: multimodal ingestion (and, as they land, experiments, environments, SDK
contracts, export/import and publishing). Registered from ``create_app`` in one block.

No ``from __future__ import annotations`` here: FastAPI resolves the ``User`` dependency from real annotations.
"""

from typing import Annotated, Any

from fastapi import File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

MAX_UPLOAD = 200 * 1024 * 1024
API_VERSION = "1"  # bumped only for a breaking change; docs/api/contract.txt is the compatibility record


class IngestChoice(BaseModel):
    key: str
    name: str | None = None
    include: bool = True


class WebhookIn(BaseModel):
    url: str
    types: list[str] = ["*"]
    description: str | None = None


class WebhookPatch(BaseModel):
    active: bool | None = None
    rotate_secret: bool = False


class ExperimentIn(BaseModel):
    name: str
    kind: str = "forecast"
    question: str | None = None
    spec: dict[str, Any] = {}


class DecideIn(BaseModel):
    choice: str
    note: str = ""


class PythonIn(BaseModel):
    code: str
    inputs: dict[str, str] = {}
    timeout: float | None = None


class SaveIn(BaseModel):
    name: str
    description: str | None = None


class IngestCommit(BaseModel):
    tables: list[IngestChoice] = []
    document: bool | None = None
    title: str | None = None


def register(app, L, User) -> None:
    @app.get("/v1/ingest/formats")
    def ingest_formats(user: User) -> dict[str, Any]:
        """File types the ingest door reads, grouped, plus the ones it refuses and why."""
        from ..ingest import formats  # noqa: PLC0415

        return formats()

    @app.post("/v1/workspaces/{ws}/ingest")
    async def ingest_inspect(request: Request, ws: str, user: User, file: Annotated[UploadFile, File()],
                             classification: Annotated[str, Form()] = "internal",
                             commit: Annotated[bool, Form()] = False) -> dict[str, Any]:
        """Upload any supported file and get back what is in it: tables (with columns, types, units, formulas,
        checked totals) and document text. ``commit=true`` loads the plan's own choices in the same call."""
        content = await file.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "file too large (200 MB at most)")
        svc = L(request).ingest
        name = file.filename or "upload"
        if commit:
            return svc.load(user, ws, content, filename=name, classification=classification)
        return svc.inspect(user, ws, content, filename=name, classification=classification)

    @app.get("/v1/workspaces/{ws}/ingest")
    def ingest_jobs(request: Request, ws: str, user: User, limit: int = 50):
        return L(request).ingest.list(user, ws, limit)

    @app.get("/v1/workspaces/{ws}/ingest/{jid}")
    def ingest_job(request: Request, ws: str, jid: str, user: User):
        return L(request).ingest.get(user, ws, jid)

    @app.post("/v1/workspaces/{ws}/ingest/{jid}/commit")
    def ingest_commit(request: Request, ws: str, jid: str, body: IngestCommit, user: User):
        return L(request).ingest.commit(user, ws, jid, tables=[c.model_dump() for c in body.tables] or None,
                                        document=body.document, title=body.title)

    @app.delete("/v1/workspaces/{ws}/ingest/{jid}")
    def ingest_discard(request: Request, ws: str, jid: str, user: User):
        return L(request).ingest.discard(user, ws, jid)

    # -- events and webhooks ----------------------------------------------------------------

    @app.middleware("http")
    async def _api_version(request: Request, call_next):
        response = await call_next(request)
        response.headers["Lyst-API-Version"] = API_VERSION
        return response

    @app.get("/v1/events/types")
    def event_types(user: User) -> dict[str, Any]:
        from .. import events  # noqa: PLC0415

        return {"schema": events.SCHEMA, "types": events.types(), "member_visible": list(events.MEMBER_TYPES)}

    @app.get("/v1/workspaces/{ws}/events")
    async def workspace_events(request: Request, ws: str, user: User, after: int = 0,
                               types: Annotated[list[str] | None, Query()] = None, limit: int = 100, wait: float = 0):
        """Events after the ``after`` cursor that this member may see; ``wait`` (seconds, at most 25) long-polls.
        Returns ``{"events": [...], "next": cursor}``; pass ``next`` back as ``after``."""
        pats = [p for t in (types or []) for p in t.split(",") if p]
        return await run_in_threadpool(L(request).events.list, user, ws, after=after, types=pats or None,
                                       limit=min(limit, 500), wait=wait)

    @app.get("/v1/workspaces/{ws}/webhooks")
    def webhooks_list(request: Request, ws: str, user: User):
        return L(request).webhooks.list(user, ws)

    @app.post("/v1/workspaces/{ws}/webhooks", status_code=201)
    def webhooks_create(request: Request, ws: str, body: WebhookIn, user: User):
        """The response carries the signing secret once; store it with the receiver."""
        return L(request).webhooks.create(user, ws, body.url, types_=body.types, description=body.description)

    @app.patch("/v1/workspaces/{ws}/webhooks/{sid}")
    def webhooks_update(request: Request, ws: str, sid: str, body: WebhookPatch, user: User):
        return L(request).webhooks.update(user, ws, sid, active=body.active, rotate_secret=body.rotate_secret)

    @app.delete("/v1/workspaces/{ws}/webhooks/{sid}")
    def webhooks_delete(request: Request, ws: str, sid: str, user: User):
        return {"deleted": L(request).webhooks.delete(user, ws, sid)}

    @app.post("/v1/workspaces/{ws}/webhooks/{sid}/ping")
    def webhooks_ping(request: Request, ws: str, sid: str, user: User):
        return L(request).webhooks.ping(user, ws, sid)

    @app.get("/v1/workspaces/{ws}/webhooks/{sid}/deliveries")
    def webhooks_deliveries(request: Request, ws: str, sid: str, user: User, status: str | None = None, limit: int = 50):
        return L(request).webhooks.deliveries(user, ws, sid, status=status, limit=limit)

    @app.post("/v1/workspaces/{ws}/webhook-deliveries/{did}/redeliver")
    def webhooks_redeliver(request: Request, ws: str, did: str, user: User):
        return L(request).webhooks.redeliver(user, ws, did)

    # -- experiments --------------------------------------------------------------------------

    @app.get("/v1/experiments/methods")
    def experiment_methods(user: User):
        from ..experiments import COMPLEXITY, DEFAULT_METHOD, METRICS  # noqa: PLC0415

        return {"forecast": COMPLEXITY, "default": DEFAULT_METHOD, "scores": list(METRICS), "kinds": ["forecast", "eval"]}

    @app.get("/v1/workspaces/{ws}/experiments")
    def experiments_list(request: Request, ws: str, user: User):
        return L(request).experiments.list(user, ws)

    @app.post("/v1/workspaces/{ws}/experiments", status_code=201)
    def experiments_create(request: Request, ws: str, body: ExperimentIn, user: User):
        return L(request).experiments.create(user, ws, name=body.name, kind=body.kind, spec=body.spec, question=body.question)

    @app.get("/v1/workspaces/{ws}/experiments/{eid}")
    def experiments_get(request: Request, ws: str, eid: str, user: User):
        return L(request).experiments.get(user, ws, eid)

    @app.post("/v1/workspaces/{ws}/experiments/{eid}/run")
    async def experiments_run(request: Request, ws: str, eid: str, user: User):
        return await run_in_threadpool(L(request).experiments.run, user, ws, eid)

    @app.get("/v1/workspaces/{ws}/experiments/{eid}/comparison")
    def experiments_compare(request: Request, ws: str, eid: str, user: User, batch: str | None = None):
        return L(request).experiments.compare(user, ws, eid, batch=batch)

    @app.post("/v1/workspaces/{ws}/experiments/{eid}/decision")
    def experiments_decide(request: Request, ws: str, eid: str, body: DecideIn, user: User):
        return L(request).experiments.decide(user, ws, eid, choice=body.choice, note=body.note)

    @app.get("/v1/workspaces/{ws}/experiment-runs/{run_id}")
    def experiment_run(request: Request, ws: str, run_id: str, user: User):
        return L(request).experiments.run_detail(user, ws, run_id)

    @app.post("/v1/workspaces/{ws}/experiment-runs/{run_id}/reproduce")
    def experiment_reproduce(request: Request, ws: str, run_id: str, user: User):
        return L(request).experiments.reproduce(user, ws, run_id)

    # -- execution environment and workbench --------------------------------------------------

    @app.get("/v1/environment")
    def environment_info(request: Request, user: User):
        """What computations run on here: interpreter and numeric package versions, and what the workbench offers."""
        return L(request).workbench.status()

    @app.post("/v1/workspaces/{ws}/workbench/sql")
    def workbench_sql(request: Request, ws: str, body: dict[str, Any], user: User):
        return L(request).workbench.sql(user, ws, str(body.get("sql", "")), max_rows=int(body.get("max_rows", 1000)))

    @app.post("/v1/workspaces/{ws}/workbench/python")
    async def workbench_python(request: Request, ws: str, body: PythonIn, user: User):
        """Run Python in the OS sandbox on inputs named as SQL queries; assign the answer to ``result``."""
        return await run_in_threadpool(L(request).workbench.python, user, ws, body.code, inputs=body.inputs, timeout=body.timeout)

    @app.get("/v1/workspaces/{ws}/workbench/runs")
    def workbench_runs(request: Request, ws: str, user: User, limit: int = 50):
        return L(request).workbench.list(user, ws, limit)

    @app.get("/v1/workspaces/{ws}/workbench/runs/{rid}")
    def workbench_run(request: Request, ws: str, rid: str, user: User):
        return L(request).workbench.get(user, ws, rid)

    @app.post("/v1/workspaces/{ws}/workbench/runs/{rid}/reproduce")
    async def workbench_reproduce(request: Request, ws: str, rid: str, user: User):
        return await run_in_threadpool(L(request).workbench.reproduce, user, ws, rid)

    @app.post("/v1/workspaces/{ws}/workbench/runs/{rid}/save")
    async def workbench_save(request: Request, ws: str, rid: str, body: SaveIn, user: User):
        return await run_in_threadpool(L(request).workbench.save, user, ws, rid, body.name, description=body.description)

