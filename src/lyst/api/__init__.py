# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Versioned HTTP API. The Streamlit UI is one client of it; nothing in the UI talks to DuckDB."""


import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from .. import observability as obs
from ..app import Lyst
from ..data.sqlguard import QueryRejected
from ..security import AccessDenied, Principal
from ..security.oidc import OIDC, OIDCError, OIDCSettings
from ..semantic import MetricSpec

log = logging.getLogger("lyst.api")

MAX_UPLOAD = 200 * 1024 * 1024


class Ask(BaseModel):
    question: str
    autonomy: str | None = None


class SQL(BaseModel):
    sql: str
    max_rows: int = 1000


class Decision(BaseModel):
    approve: bool


class ScheduleIn(BaseModel):
    title: str
    question: str
    every_seconds: int


class Term(BaseModel):
    term: str
    definition: str
    maps_to_metric: str | None = None
    source: str | None = None


class WorkspacePatch(BaseModel):
    allow_external_llm: bool | None = None
    autonomy: str | None = None


class SkillIn(BaseModel):
    name: str
    purpose: str
    source: str
    input_sql: str
    tests: list[dict[str, Any]]
    triggers: list[str]
    params: dict[str, Any] | None = None


class MemberIn(BaseModel):
    email: str
    role: str


class EntityIn(BaseModel):
    kind: str
    name: str
    description: str = ""
    owner: str | None = None
    attributes: dict[str, Any] | None = None


class RelationIn(BaseModel):
    from_kind: str
    from_name: str
    rel: str
    to_kind: str
    to_name: str
    evidence: dict[str, Any] | None = None


class Correction(BaseModel):
    content: str
    analysis_id: str | None = None
    layer: str = "team"


def create_app(lyst: Lyst | None = None, *, start_workers: bool = True, oidc: OIDC | None = None) -> FastAPI:
    obs.configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lyst = lyst or Lyst.build()
        settings = OIDCSettings.from_env()
        app.state.oidc = oidc or (OIDC(settings) if settings else None)
        if start_workers:
            app.state.lyst.runtime.start(app.state.lyst.settings.workers)
        obs.WORKERS.set(len(app.state.lyst.runtime._threads))
        log.info("lyst api ready", extra={"workers": len(app.state.lyst.runtime._threads),
                                          "oidc": bool(app.state.oidc), "model": bool(app.state.lyst.llm.info())})
        yield
        if lyst is None:
            app.state.lyst.close()
        else:
            app.state.lyst.runtime.stop()

    app = FastAPI(title="Lyst API", version="0.1.0", lifespan=lifespan)

    def L(request: Request) -> Lyst:
        return request.app.state.lyst

    def me(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        return L(request).guard.authenticate(token)

    User = Annotated[Principal, Depends(me)]

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or obs.new_request_id()
        token = obs.request_id.set(rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = rid
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", request.url.path)
            if route not in ("/metrics", "/health"):
                obs.HTTP_REQUESTS.labels(route, request.method, str(status)).inc()
                obs.HTTP_LATENCY.labels(route).observe(time.perf_counter() - start)
                if status >= 500:
                    log.error("request failed", extra={"route": route, "status": status})
            obs.request_id.reset(token)

    @app.exception_handler(AccessDenied)
    async def _denied(_: Request, exc: AccessDenied):
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)

    @app.exception_handler(QueryRejected)
    async def _rejected(_: Request, exc: QueryRejected):
        return JSONResponse({"error": "query_rejected", "detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError):
        return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _invalid(_: Request, exc: ValueError):
        return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=400)

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        lyst = L(request)
        lyst.stores.control.scalar("SELECT 1")
        info = lyst.llm.info()
        queued = lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE status = 'queued'") or 0
        obs.QUEUE_DEPTH.set(queued)
        return {"status": "ok", "version": "0.1.0", "encrypted": lyst.settings.encrypt,
                "model": info.name if info else None, "workers": len(lyst.runtime._threads), "queued": queued,
                "oidc": request.app.state.oidc is not None}

    @app.get("/metrics")
    def prometheus_metrics(request: Request):
        lyst = L(request)
        obs.QUEUE_DEPTH.set(lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE status = 'queued'") or 0)
        if lyst.llm.gateway:
            obs.INFLIGHT.set(lyst.llm.gateway.admission.counts.total)
        return Response(generate_latest(obs.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # -- sign-in ----------------------------------------------------------------------------

    @app.get("/auth/login")
    def auth_login(request: Request, return_to: str | None = None):
        o: OIDC | None = request.app.state.oidc
        if not o:
            raise HTTPException(404, "OIDC is not configured; use an API token")
        ui = os.environ.get("LYST_UI_URL", "http://localhost:8501")
        target = return_to if return_to and return_to.startswith(ui) else ui
        return RedirectResponse(o.login_url(target), status_code=302)

    @app.get("/auth/callback")
    def auth_callback(request: Request, state: str, code: str):
        o: OIDC | None = request.app.state.oidc
        if not o:
            raise HTTPException(404, "OIDC is not configured")
        lyst = L(request)
        try:
            claims, return_to = o.complete(state, code)
        except OIDCError as exc:
            lyst.guard.audit(Principal("anonymous", ""), None, "auth.oidc", None, "denied", {"reason": str(exc)})
            return PlainTextResponse(f"Sign-in failed: {exc}", status_code=403)
        user = lyst.guard.user_from_oidc(claims["sub"], claims["email"].lower(), claims.get("name") or "")
        token = lyst.guard.issue_token(user, "oidc", hours=o.settings.session_hours)
        return RedirectResponse(return_to + ("&" if "?" in return_to else "?") + urlencode({"token": token}),
                                status_code=302)

    @app.post("/auth/logout")
    def auth_logout(request: Request, user: User):
        auth = request.headers.get("authorization", "")
        L(request).guard.revoke_token(auth[7:])
        return {"ok": True}

    @app.get("/v1/me")
    def whoami(request: Request, user: User):
        return {"user_id": user.user_id, "email": user.email,
                "workspaces": L(request).guard.workspaces_for(user)}

    @app.get("/v1/workspaces/{ws}/members")
    def members(request: Request, ws: str, user: User):
        return L(request).guard.members(user, ws)

    @app.post("/v1/workspaces/{ws}/members")
    def add_member(request: Request, ws: str, body: MemberIn, user: User):
        g = L(request).guard
        target = g.user_by_email(body.email)
        if not target:
            raise KeyError(f"no user with email {body.email}; they must sign in once first")
        g.add_member(user, ws, target.user_id, body.role)
        return {"ok": True}

    @app.delete("/v1/workspaces/{ws}/members/{user_id}")
    def remove_member(request: Request, ws: str, user_id: str, user: User):
        L(request).guard.remove_member(user, ws, user_id)
        return {"ok": True}

    # -- data -------------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/datasets")
    def datasets(request: Request, ws: str, user: User):
        return L(request).data.catalog(user, ws)

    @app.post("/v1/workspaces/{ws}/datasets")
    async def upload(request: Request, ws: str, user: User, name: Annotated[str, Form()],
                     file: Annotated[UploadFile, File()], description: Annotated[str | None, Form()] = None):
        content = await file.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        return L(request).data.ingest(user, ws, name, content, filename=file.filename or "upload.csv",
                                      description=description)

    @app.get("/v1/workspaces/{ws}/datasets/{name}/profile")
    def profile(request: Request, ws: str, name: str, user: User):
        return L(request).data.dataset_profile(user, ws, name)

    @app.post("/v1/workspaces/{ws}/query")
    def query(request: Request, ws: str, body: SQL, user: User):
        r = L(request).data.query(user, ws, body.sql, max_rows=min(body.max_rows, 10_000))
        return {"columns": r.columns, "rows": r.rows, "executed_sql": r.executed_sql, "ms": r.ms,
                "query_id": r.query_id, "datasets": r.datasets}

    @app.get("/v1/workspaces/{ws}/queries")
    def queries(request: Request, ws: str, user: User):
        return L(request).data.history(user, ws)

    # -- semantic ---------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/metrics")
    def metrics(request: Request, ws: str, user: User):
        return [m.model_dump() for m in L(request).semantic.metrics(user, ws)]

    @app.post("/v1/workspaces/{ws}/metrics")
    def define_metric(request: Request, ws: str, spec: MetricSpec, user: User):
        return {"name": spec.name, "version": L(request).semantic.define(user, ws, spec)}

    @app.get("/v1/workspaces/{ws}/glossary")
    def glossary(request: Request, ws: str, user: User):
        return L(request).semantic.glossary(user, ws)

    @app.post("/v1/workspaces/{ws}/glossary")
    def set_term(request: Request, ws: str, body: Term, user: User):
        L(request).semantic.set_term(user, ws, body.term, body.definition, body.maps_to_metric, body.source)
        return {"ok": True}

    # -- analyst and work -------------------------------------------------------------------

    @app.post("/v1/workspaces/{ws}/tasks", status_code=202)
    def ask(request: Request, ws: str, body: Ask, user: User):
        return {"task_id": L(request).runtime.submit(user, ws, body.question, autonomy=body.autonomy)}

    @app.get("/v1/workspaces/{ws}/tasks")
    def tasks(request: Request, ws: str, user: User):
        return L(request).runtime.list(user, ws)

    @app.get("/v1/tasks/{task_id}")
    def task(request: Request, task_id: str, user: User):
        return L(request).runtime.get(user, task_id)

    @app.post("/v1/tasks/{task_id}/{action}")
    def control(request: Request, task_id: str, action: str, user: User):
        rt = L(request).runtime
        fn = {"pause": rt.pause, "resume": rt.resume, "cancel": rt.cancel, "retry": rt.retry}.get(action)
        if not fn:
            raise HTTPException(404, "unknown action")
        fn(user, task_id)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/approvals")
    def approvals(request: Request, ws: str, user: User, status: str = "pending"):
        return L(request).runtime.approvals(user, ws, status)

    @app.post("/v1/approvals/{approval_id}")
    def decide(request: Request, approval_id: str, body: Decision, user: User):
        return L(request).runtime.decide(user, approval_id, body.approve)

    @app.get("/v1/workspaces/{ws}/schedules")
    def schedules(request: Request, ws: str, user: User):
        return L(request).runtime.schedules(user, ws)

    @app.post("/v1/workspaces/{ws}/schedules")
    def schedule(request: Request, ws: str, body: ScheduleIn, user: User):
        return L(request).runtime.create_schedule(user, ws, body.title, body.question, body.every_seconds)

    # -- intelligence -----------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/memory")
    def memory(request: Request, ws: str, user: User, layer: str | None = None):
        return L(request).memory.list(user, ws, layer)

    @app.post("/v1/workspaces/{ws}/memory/corrections")
    def correct(request: Request, ws: str, body: Correction, user: User):
        mid = L(request).memory.remember(user, ws, body.layer, body.content, trust="user_validated",
                                         provenance={"kind": "correction", "analysis_id": body.analysis_id})
        return {"memory_id": mid}

    @app.post("/v1/workspaces/{ws}/memory/{memory_id}/promote")
    def promote(request: Request, ws: str, memory_id: str, user: User):
        L(request).memory.promote(user, ws, memory_id)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/entities")
    def entities(request: Request, ws: str, user: User, kind: str | None = None):
        return L(request).knowledge.entities(user, ws, kind)

    @app.post("/v1/workspaces/{ws}/entities")
    def upsert_entity(request: Request, ws: str, body: EntityIn, user: User):
        L(request).knowledge.upsert_entity(user, ws, body.kind, body.name, description=body.description,
                                           owner=body.owner, attributes=body.attributes)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/relations")
    def relations(request: Request, ws: str, user: User, kind: str | None = None, name: str | None = None):
        return L(request).knowledge.relations(user, ws, kind, name)

    @app.post("/v1/workspaces/{ws}/relations")
    def relate(request: Request, ws: str, body: RelationIn, user: User):
        L(request).knowledge.relate(user, ws, body.from_kind, body.from_name, body.rel, body.to_kind, body.to_name,
                                    evidence=body.evidence, source="user")
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/lineage/{kind}/{name}")
    def lineage(request: Request, ws: str, kind: str, name: str, user: User):
        return L(request).knowledge.lineage(user, ws, kind, name)

    @app.get("/v1/workspaces/{ws}/documents")
    def documents(request: Request, ws: str, user: User):
        return L(request).knowledge.documents(user, ws)

    @app.post("/v1/workspaces/{ws}/documents")
    async def add_document(request: Request, ws: str, user: User, file: Annotated[UploadFile, File()],
                           title: Annotated[str | None, Form()] = None, min_role: Annotated[str, Form()] = "viewer",
                           classification: Annotated[str, Form()] = "internal"):
        content = await file.read(MAX_UPLOAD + 1)
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        return L(request).knowledge.add_document(user, ws, content, filename=file.filename or "document.txt", title=title,
                                                 classification=classification, min_role=min_role)

    @app.get("/v1/workspaces/{ws}/documents/search")
    def search_documents(request: Request, ws: str, user: User, q: str, limit: int = 5):
        return L(request).knowledge.search(user, ws, q, limit=min(limit, 20))

    @app.get("/v1/workspaces/{ws}/analyses")
    def analyses(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.read")
        return lyst.stores.workspace(ws).query(
            "SELECT id, task_id, question, answer, created_by, created_at FROM analyses ORDER BY created_at DESC LIMIT 100")

    @app.get("/v1/workspaces/{ws}/evals")
    def eval_runs(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.read")
        return lyst.stores.control.query(
            "SELECT id, label, model, cases, passed, p95_seconds, created_at FROM eval_runs WHERE workspace_id = ? "
            "ORDER BY created_at DESC LIMIT 50", [ws])

    # -- build: skills and deliverables -----------------------------------------------------

    @app.get("/v1/workspaces/{ws}/skills")
    def skills(request: Request, ws: str, user: User):
        return L(request).skills.list(user, ws)

    @app.get("/v1/workspaces/{ws}/skills/{name}")
    def skill(request: Request, ws: str, name: str, user: User, version: int | None = None):
        return L(request).skills.get(user, ws, name, version)

    @app.post("/v1/workspaces/{ws}/skills")
    def propose_skill(request: Request, ws: str, body: SkillIn, user: User):
        return L(request).skills.propose(user, ws, **body.model_dump())

    @app.post("/v1/workspaces/{ws}/skills/{name}/{version}/test")
    def test_skill(request: Request, ws: str, name: str, version: int, user: User):
        return L(request).skills.test(user, ws, name, version)

    @app.post("/v1/workspaces/{ws}/skills/{name}/{version}/register")
    def register_skill(request: Request, ws: str, name: str, version: int, user: User):
        lyst = L(request)
        return {"approval_id": lyst.skills.request_registration(user, ws, name, version, lyst.runtime)}

    @app.post("/v1/workspaces/{ws}/skills/{name}/retire")
    def retire_skill(request: Request, ws: str, name: str, user: User):
        L(request).skills.retire(user, ws, name)
        return {"ok": True}

    @app.post("/v1/tasks/{task_id}/artifacts/{kind}")
    def make_artifact(request: Request, task_id: str, kind: str, user: User):
        lyst = L(request)
        return lyst.artifacts.build(user, lyst.runtime.get(user, task_id), kind)

    @app.get("/v1/tasks/{task_id}/artifacts")
    def task_artifacts(request: Request, task_id: str, user: User):
        lyst = L(request)
        t = lyst.runtime.get(user, task_id)
        return lyst.artifacts.list(user, t["workspace_id"], task_id)

    @app.get("/v1/artifacts/{artifact_id}")
    def download_artifact(request: Request, artifact_id: str, user: User):
        meta, data = L(request).artifacts.get(user, artifact_id)
        mime = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "md": "text/markdown"}[meta["kind"]]
        name = {"xlsx": "analysis.xlsx", "md": "briefing.md"}[meta["kind"]]
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/v1/capabilities")
    def capabilities(request: Request, user: User):
        return L(request).services.registry.describe()

    # -- settings ---------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/audit")
    def audit(request: Request, ws: str, user: User, limit: int = 200):
        lyst = L(request)
        lyst.guard.require(user, ws, "audit.read")
        return lyst.stores.control.query(
            "SELECT occurred_at, actor, action, target, outcome, detail FROM audit_log WHERE workspace_id = ? "
            "ORDER BY id DESC LIMIT ?", [ws, min(limit, 1000)])

    @app.get("/v1/workspaces/{ws}/settings")
    def settings(request: Request, ws: str, user: User):
        lyst = L(request)
        role = lyst.guard.require(user, ws, "workspace.read")
        w = lyst.stores.control.query("SELECT * FROM workspaces WHERE id = ?", [ws])[0]
        info = lyst.llm.info()
        calls = lyst.stores.control.query(
            "SELECT count(*) AS calls, coalesce(sum(input_tokens), 0) AS input_tokens, "
            "coalesce(sum(output_tokens), 0) AS output_tokens FROM llm_calls WHERE workspace_id = ?", [ws])[0]
        return {"workspace": w, "role": role, "model": info.__dict__ if info else None, "llm_usage": calls,
                "gateway": lyst.llm.status() if role in ("admin", "owner") else None,
                "encrypted": lyst.settings.encrypt, "home": str(lyst.settings.home) if role == "owner" else None}

    @app.patch("/v1/workspaces/{ws}/settings")
    def patch_settings(request: Request, ws: str, body: WorkspacePatch, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.admin")
        if body.autonomy is not None and body.autonomy not in ("assist", "delegate", "autonomous", "governed"):
            raise ValueError("unknown autonomy mode")
        for col, val in (("allow_external_llm", body.allow_external_llm), ("autonomy", body.autonomy)):
            if val is not None:
                lyst.stores.control.execute(f"UPDATE workspaces SET {col} = ? WHERE id = ?", [val, ws])
                lyst.guard.audit(user, ws, f"settings.{col}", ws, "ok", {"value": val})
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/model-usage")
    def model_usage(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "audit.read")
        return lyst.stores.control.query(
            "SELECT coalesce(workload, purpose) AS workload, model, provider, count(*) AS calls, "
            "count(*) FILTER (WHERE outcome = 'ok') AS ok, coalesce(sum(input_tokens), 0) AS input_tokens, "
            "coalesce(sum(output_tokens), 0) AS output_tokens, round(coalesce(sum(cost_usd), 0), 6) AS cost_usd, "
            "quantile_cont(latency_ms, 0.95) AS p95_ms FROM llm_calls WHERE workspace_id = ? GROUP BY ALL ORDER BY calls DESC",
            [ws])

    return app


def main() -> None:  # pragma: no cover - process entry
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("LYST_HOST", "127.0.0.1"),
                port=int(os.environ.get("LYST_PORT", "8770")))
