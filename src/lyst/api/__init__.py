"""Versioned HTTP API. The Streamlit UI is one client of it; nothing in the UI talks to DuckDB."""


import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..app import Lyst
from ..data.sqlguard import QueryRejected
from ..security import AccessDenied, Principal
from ..semantic import MetricSpec

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


class Correction(BaseModel):
    content: str
    analysis_id: str | None = None
    layer: str = "team"


def create_app(lyst: Lyst | None = None, *, start_workers: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lyst = lyst or Lyst.build()
        if start_workers:
            app.state.lyst.runtime.start(app.state.lyst.settings.workers)
        yield
        if lyst is None:
            app.state.lyst.close()
        else:
            app.state.lyst.runtime.stop()

    app = FastAPI(title="Lyst API", version="0.1.0", lifespan=lifespan)

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

    def L(request: Request) -> Lyst:
        return request.app.state.lyst

    def me(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        return L(request).guard.authenticate(token)

    User = Annotated[Principal, Depends(me)]

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        lyst = L(request)
        lyst.stores.control.scalar("SELECT 1")
        info = lyst.llm.info()
        return {"status": "ok", "version": "0.1.0", "encrypted": lyst.settings.encrypt,
                "model": info.name if info else None, "workers": len(lyst.runtime._threads)}

    @app.get("/v1/me")
    def whoami(request: Request, user: User):
        return {"user_id": user.user_id, "email": user.email,
                "workspaces": L(request).guard.workspaces_for(user)}

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

    @app.get("/v1/workspaces/{ws}/analyses")
    def analyses(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.read")
        return lyst.stores.workspace(ws).query(
            "SELECT id, task_id, question, answer, created_by, created_at FROM analyses ORDER BY created_at DESC LIMIT 100")

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
