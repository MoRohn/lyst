# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Versioned HTTP API. The Streamlit UI is one client of it; nothing in the UI talks to DuckDB."""


import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from .. import decisions as decision_mod
from .. import observability as obs
from .. import operations
from ..app import Lyst
from ..connectors import ConnectorError, manifests
from ..contracts import ContractSpec
from ..data.sqlguard import QueryRejected
from ..followups import FollowUps
from ..operations import QuotaExceeded
from ..secrets import MasterKeyMissing, ReauthRequired, SecretUnavailable
from ..secrets import providers as credential_providers
from ..security import AccessDenied, Principal
from ..security.oidc import OIDC, OIDCError, OIDCSettings, oidc_settings_from_credentials
from ..semantic import MetricSpec

log = logging.getLogger("lyst.api")

MAX_UPLOAD = 200 * 1024 * 1024


class ScheduleWorkflow(BaseModel):
    every_seconds: int


class RunWorkflow(BaseModel):
    period: str | None = None


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
    every_seconds: int | None = None
    at: str | None = None  # local time of day, HH:MM (calendar schedule)
    weekdays: list[str] | None = None  # e.g. ["mon"]; empty = every day
    timezone: str | None = None  # IANA name, e.g. Europe/London
    overlap: str = "skip"


class ScenarioModelIn(BaseModel):
    name: str
    spec: dict[str, Any]
    note: str = ""


class ScenarioRun(BaseModel):
    overrides: dict[str, Any] = {}
    version: int | None = None
    previous: dict[str, Any] | None = None  # an earlier result: only changed nodes are recomputed


class ScenarioAnalysisIn(BaseModel):
    overrides: dict[str, Any] = {}
    output: str | None = None
    draws: int = 10_000
    seed: int = 7
    thresholds: dict[str, float] | None = None


class ScenarioSave(BaseModel):
    name: str
    overrides: dict[str, Any] = {}
    note: str = ""


class WhatIf(BaseModel):
    question: str
    model: str | None = None


class DecisionIn(BaseModel):
    title: str
    spec: dict[str, Any]
    task_id: str | None = None  # the task or answer this decision follows from


class DecisionSubmit(BaseModel):
    chosen: str
    rationale: str = ""


class DecisionReview(BaseModel):
    observed: float
    note: str = ""


class AllocateIn(BaseModel):
    items: list[dict[str, Any]]
    budget: float
    groups: dict[str, int] | None = None
    integer: bool = True


class FrameIn(BaseModel):
    question: str


class FollowUpIn(BaseModel):
    text: str
    timezone: str | None = None  # for "schedule this every Monday at 8"


class QuotasIn(BaseModel):
    max_running: int | None = None
    max_queued: int | None = None
    monthly_spend_usd: float | None = None
    monthly_tokens: int | None = None
    task_budget_seconds: int | None = None


class Term(BaseModel):
    term: str
    definition: str
    maps_to_metric: str | None = None
    source: str | None = None


class WorkspacePatch(BaseModel):
    allow_external_llm: bool | None = None
    autonomy: str | None = None
    webhook_url: str | None = None
    llm_tier: str | None = None    # economy | balanced | frontier; "" follows the installation
    llm_model: str | None = None   # an OpenRouter slug used alone; "" or "auto" walks the tier


class MonitorIn(BaseModel):
    metric: str
    grain: str = "month"
    threshold_sigma: float = 3.0
    every_seconds: int = 3600
    title: str | None = None
    investigate: bool = True


class FindingStatus(BaseModel):
    status: str


class SkillIn(BaseModel):
    name: str
    purpose: str
    source: str
    input_sql: str
    tests: list[dict[str, Any]]
    triggers: list[str]
    params: dict[str, Any] | None = None


class ConnectionIn(BaseModel):
    kind: str
    name: str
    values: dict[str, Any]


class SyncIn(BaseModel):
    source_table: str
    dataset: str | None = None
    cursor_column: str | None = None
    every_seconds: int | None = None


class CredentialIn(BaseModel):
    provider: str
    name: str
    secret: str = ""
    fields: dict[str, Any] = {}
    scope_kind: str = "installation"      # installation | workspace | user
    scope_id: str | None = None           # the workspace id; the caller's own id for user scope
    test_first: bool = True


class ReplaceIn(BaseModel):
    secret: str
    fields: dict[str, Any] = {}


class PolicyIn(BaseModel):
    provider: str
    workspace_override: bool | None = None
    user_credentials: bool | None = None


class InstallationPatch(BaseModel):
    settings: dict[str, str | None] | None = None   # LYST_LLM_TIER, LYST_LLM_MODEL, LYST_EMBED_MODEL
    policy: PolicyIn | None = None
    add_admin: str | None = None                    # a user id


class SetupIn(BaseModel):
    recovery_key: str | None = None   # paste a saved master key to recover an installation; empty generates a new one


class RowPolicyIn(BaseModel):
    column: str
    principal_kind: str   # user | role
    principal: str        # a user id, or viewer | editor | admin | owner
    allowed_values: list[Any]


class MemberIn(BaseModel):
    email: str
    role: str


class ContractIn(BaseModel):
    spec: ContractSpec
    note: str = ""


class DeprecateIn(BaseModel):
    replaced_by: str | None = None
    note: str = ""


class CommentIn(BaseModel):
    body: str
    kind: str = "comment"
    parent_id: str | None = None


class ResolveIn(BaseModel):
    note: str | None = None


class AssignIn(BaseModel):
    object_kind: str
    object_id: str
    assignee: str
    note: str = ""
    due: str | None = None


class CompleteIn(BaseModel):
    note: str = ""


class ProposeIn(BaseModel):
    action: str
    object_kind: str
    object_id: str
    payload: dict[str, Any] = {}
    summary: str
    reviewer: str | None = None


class DecideIn(BaseModel):
    approve: bool
    note: str = ""


class ShareIn(BaseModel):
    visibility: str | None = None
    users: list[str] = []


class TransferIn(BaseModel):
    object_kind: str
    object_id: str
    to_user: str
    note: str = ""


class TransformIn(BaseModel):
    name: str
    sql: str
    title: str | None = None
    description: str = ""
    key: list[str] = []
    incremental_column: str | None = None
    note: str = ""


class PreviewIn(BaseModel):
    sql: str
    key: list[str] = []


class DesignIn(BaseModel):
    request: str


class AuthorityIn(BaseModel):
    level: str | None = None
    subject: str = ""
    note: str = ""


class DraftIn(BaseModel):
    name: str
    openapi: dict[str, Any]
    base_url: str | None = None


class DraftEdit(BaseModel):
    config: dict[str, Any] = {}
    cursor_column: str | None = None


class DraftToken(BaseModel):
    token: str | None = None
    note: str = ""


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

    async def _run_mcp(mcp, ready: asyncio.Event, stop: asyncio.Event) -> None:
        # the session manager's cancel scope must be entered and exited by the same task, whether the engine starts
        # with the process or later from the setup page
        async with mcp.session_manager.run():
            ready.set()
            await stop.wait()

    async def _bring_up(app: FastAPI) -> None:
        """Open the stores and start everything. Raises MasterKeyMissing when no trust anchor holds the key."""
        app.state.lyst = lyst or await run_in_threadpool(Lyst.build)
        settings = OIDCSettings.from_env() or oidc_settings_from_credentials(app.state.lyst.credentials)
        app.state.oidc = oidc or (OIDC(settings) if settings else None)
        if start_workers:
            app.state.lyst.runtime.start(app.state.lyst.settings.workers)
        obs.WORKERS.set(len(app.state.lyst.runtime._threads))
        from mcp.server.transport_security import TransportSecuritySettings  # noqa: PLC0415

        from ..mcp_server import build_mcp  # noqa: PLC0415

        hosts = [h.strip() for h in os.environ.get("LYST_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",") if h.strip()]
        mcp = build_mcp(app.state.lyst)
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/", stateless_http=True, json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                         allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]))
        app.mount("/mcp", mcp_app)
        app.state.mcp = mcp
        ready, app.state.mcp_stop = asyncio.Event(), asyncio.Event()
        app.state.mcp_task = asyncio.create_task(_run_mcp(mcp, ready, app.state.mcp_stop))
        await ready.wait()
        app.state.setup_reason = None
        log.info("lyst api ready", extra={"workers": len(app.state.lyst.runtime._threads),
                                          "oidc": bool(app.state.oidc), "model": bool(app.state.lyst.llm.info())})

    async def _tear_down(app: FastAPI) -> None:
        task = getattr(app.state, "mcp_task", None)
        if task is not None:
            app.state.mcp_stop.set()
            await task
            app.state.mcp_task = None
        if app.state.lyst is not None:
            if lyst is None:
                app.state.lyst.close()
            else:
                app.state.lyst.runtime.stop()
            app.state.lyst = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lyst, app.state.oidc, app.state.setup_reason, app.state.mcp_task = None, None, None, None
        app.state.setup_lock = asyncio.Lock()
        try:
            await _bring_up(app)
        except MasterKeyMissing as exc:
            app.state.setup_reason = str(exc)
            log.warning("setup mode: no master encryption key; waiting for initialisation from the browser or an operator")
        try:
            yield
        finally:
            await _tear_down(app)

    app = FastAPI(title="Lyst API", version="0.1.0", lifespan=lifespan)

    def L(request: Request) -> Lyst:
        built = request.app.state.lyst
        if built is None:
            raise HTTPException(503, {"error": "setup_required", "detail": "Lyst has no master encryption key yet; open the interface "
                                                                           "on the engine host to initialise it."})
        return built

    def _loopback(request: Request) -> bool:
        host = request.client.host if request.client else ""
        return host in ("127.0.0.1", "::1", "localhost", "testclient")

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
        from opentelemetry import context as otel_context  # noqa: PLC0415
        from opentelemetry import trace as otel_trace  # noqa: PLC0415

        span = obs.tracer().start_span("http")
        otel_token = otel_context.attach(otel_trace.set_span_in_context(span))
        abandoned = False
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = rid
            return response
        except (GeneratorExit, asyncio.CancelledError):
            # the request was abandoned (client gone, server shutting down); this coroutine may be finalised in
            # another context, where detaching would fail and log an error, so only end the span
            abandoned = True
            raise
        finally:
            span.set_attribute("http.route", getattr(request.scope.get("route"), "path", request.url.path))
            span.set_attribute("http.status_code", status)
            span.set_attribute("lyst.request_id", rid)
            span.end()
            if not abandoned:
                otel_context.detach(otel_token)
            route = getattr(request.scope.get("route"), "path", request.url.path)
            if route not in ("/metrics", "/health"):
                obs.HTTP_REQUESTS.labels(route, request.method, str(status)).inc()
                obs.HTTP_LATENCY.labels(route).observe(time.perf_counter() - start)
                if status >= 500:
                    log.error("request failed", extra={"route": route, "status": status})
            if not abandoned:
                obs.request_id.reset(token)

    @app.exception_handler(AccessDenied)
    async def _denied(_: Request, exc: AccessDenied):
        return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)

    @app.exception_handler(ReauthRequired)
    async def _reauth(_: Request, exc: ReauthRequired):
        return JSONResponse({"error": "reauth_required", "detail": str(exc)}, status_code=401)

    @app.exception_handler(MasterKeyMissing)
    async def _master_key(_: Request, exc: MasterKeyMissing):
        return JSONResponse({"error": "setup_conflict", "detail": str(exc)}, status_code=409)

    @app.exception_handler(SecretUnavailable)
    async def _secret_unavailable(_: Request, exc: SecretUnavailable):
        return JSONResponse({"error": "secret_unavailable", "detail": str(exc)}, status_code=503)

    @app.exception_handler(QueryRejected)
    async def _rejected(_: Request, exc: QueryRejected):
        return JSONResponse({"error": "query_rejected", "detail": str(exc)}, status_code=400)

    @app.exception_handler(ConnectorError)
    async def _connector(_: Request, exc: ConnectorError):
        return JSONResponse({"error": "connector", "detail": str(exc)}, status_code=400)

    @app.exception_handler(QuotaExceeded)
    async def _quota(_: Request, exc: QuotaExceeded):
        return JSONResponse({"error": "quota_exceeded", "detail": str(exc)}, status_code=429,
                            headers={"Retry-After": str(exc.retry_after)})

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError):
        return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def _invalid(_: Request, exc: ValueError):
        return JSONResponse({"error": "invalid", "detail": str(exc)}, status_code=400)

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        if request.app.state.lyst is None:
            return {"status": "setup", "version": "0.1.0", "setup_required": True, "oidc": False}
        lyst = L(request)
        lyst.stores.control.scalar("SELECT 1")
        info = lyst.llm.info()
        queued = lyst.stores.control.scalar("SELECT count(*) FROM tasks WHERE status = 'queued'") or 0
        obs.QUEUE_DEPTH.set(queued)
        return {"status": "ok", "version": "0.1.0", "encrypted": lyst.settings.encrypt, "setup_required": False,
                "model": info.name if info else None, "workers": len(lyst.runtime._threads), "queued": queued,
                "oidc": request.app.state.oidc is not None}

    # -- first run: initialise encryption (loopback only, before any data exists) --------------

    @app.get("/setup/status")
    def setup_status(request: Request) -> dict[str, Any]:
        from ..secrets import masterkey  # noqa: PLC0415

        return {"setup_required": request.app.state.lyst is None, "reason": request.app.state.setup_reason,
                "keyring": masterkey.keyring_available(), "loopback": _loopback(request),
                "openbao": bool(os.environ.get("LYST_OPENBAO_URL"))}

    @app.post("/setup/initialize")
    async def setup_initialize(request: Request, body: SetupIn) -> dict[str, Any]:
        """Generate the master key into the OS credential store (or restore a saved one) and start the engine.
        The recovery copy is returned exactly once and never stored by Lyst."""
        from ..secrets import masterkey  # noqa: PLC0415

        if not _loopback(request):
            raise AccessDenied("initialise encryption from the machine that runs the engine (loopback only)")
        async with request.app.state.setup_lock:  # two clicks at once: the second sees the first's result
            if request.app.state.lyst is not None:
                raise ValueError("Lyst is already initialised")
            fresh = not body.recovery_key
            mk = masterkey.provision_into_keyring() if fresh else masterkey.store_into_keyring(body.recovery_key)
            try:
                await _bring_up(request.app)
            except Exception as exc:
                masterkey.remove_from_keyring()
                await _tear_down(request.app)
                sealed_elsewhere = (not fresh) or "encryption key" in str(exc).lower()
                raise ValueError("the existing databases were sealed with a different master key; restore its recovery copy instead"
                                 if sealed_elsewhere else f"initialisation failed: {exc}") from exc
        return {"initialised": True, "source": mk.source, "recovery_key": mk.key.hex() if fresh else None}

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

    # -- credentials: users manage connections, Lyst manages secrets ---------------------------

    def _scope(user: Principal, scope_kind: str, scope_id: str | None) -> tuple[str, str | None]:
        if scope_kind == "user":
            return "user", user.user_id
        if scope_kind == "installation":
            return "installation", None
        return "workspace", scope_id

    @app.get("/v1/credentials/providers")
    def credential_providers_list(user: User):
        return [{"id": p.id, "label": p.label, "secret_label": p.secret_label, "scopes": list(p.scopes), "notes": p.notes,
                 "short_lived": p.short_lived, "revocation_note": p.revocation_note,
                 "fields": [{"name": n, "label": label, "required": req} for n, label, req in p.fields]}
                for p in credential_providers.PROVIDERS.values()]

    @app.get("/v1/credentials")
    def credentials_list(request: Request, user: User, scope_kind: str = "installation", scope_id: str | None = None):
        lyst = L(request)
        kind, sid = _scope(user, scope_kind, scope_id)
        return {"scope_kind": kind, "scope_id": sid, "credentials": lyst.credentials.list(user, scope_kind=kind, scope_id=sid),
                "policy": lyst.credentials.policy()}

    @app.post("/v1/credentials")
    def credentials_connect(request: Request, body: CredentialIn, user: User):
        kind, sid = _scope(user, body.scope_kind, body.scope_id)
        return L(request).credentials.connect(user, scope_kind=kind, scope_id=sid, provider=body.provider, name=body.name,
                                              secret=body.secret, fields=body.fields, test_first=body.test_first)

    @app.post("/v1/credentials/{cid}/test")
    def credentials_test(request: Request, cid: str, user: User):
        return L(request).credentials.test(user, cid)

    @app.post("/v1/credentials/{cid}/replace")
    def credentials_replace(request: Request, cid: str, body: ReplaceIn, user: User):
        return L(request).credentials.replace(user, cid, body.secret, fields=body.fields)

    @app.delete("/v1/credentials/{cid}")
    def credentials_revoke(request: Request, cid: str, user: User):
        return L(request).credentials.revoke(user, cid)

    @app.get("/v1/credentials/{cid}/history")
    def credentials_history(request: Request, cid: str, user: User):
        return L(request).credentials.history(user, cid)

    @app.get("/v1/installation")
    def installation(request: Request, user: User):
        """Installation-wide state for its administrators: who they are, the policy, settings, the secret backend."""
        from ..security import vault  # noqa: PLC0415

        lyst = L(request)
        c = lyst.credentials
        if not c.is_installation_admin(user):
            raise AccessDenied("installation settings are managed by installation administrators")
        st = c.status()
        return {"admins": st["admins"], "policy": c.policy(), "settings": c.settings_all(), "backend": st["backend"],
                "audit": st["audit"], "active_credentials": st["active"],
                "master_key_source": lyst.settings.master_key_source, "encrypted": lyst.settings.encrypt,
                "anchors": vault.status(), "home": str(lyst.settings.home), "provider": lyst.settings.llm.provider,
                "model": (lyst.llm.info().__dict__ if lyst.llm.info() else None),
                "events": c.events(user, scope_kind="installation", limit=50)}

    @app.post("/v1/installation/backup")
    def installation_backup(request: Request, user: User):
        """B3: an online backup taken by the engine that owns the files (DuckDB lets no other process open them while
        it runs). Installation administrators only; written under <home>/backups, never to a caller-chosen path."""

        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("backups are taken by installation administrators")
        from .. import continuity  # noqa: PLC0415

        # atomic: written to a partial folder and renamed only when complete (a full disk leaves no half backup)
        return continuity.take(lyst, actor=user)

    @app.get("/v1/installation/continuity")
    def installation_continuity(request: Request, user: User):
        """Backups, the last restore drill, and recovery objectives as measured (installation administrators)."""
        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("continuity is for installation administrators")
        from .. import continuity  # noqa: PLC0415

        return continuity.report(lyst)

    @app.post("/v1/installation/backups/{name}/drill")
    def installation_drill(request: Request, name: str, user: User):
        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("restore drills are run by installation administrators")
        from .. import continuity  # noqa: PLC0415

        if not continuity.STAMP.match(name):
            raise HTTPException(404, "unknown backup")
        path = lyst.settings.home / "backups" / name
        if not (path / "manifest.json").exists():
            raise HTTPException(404, "unknown backup")
        return continuity.drill(lyst.settings, path)

    @app.patch("/v1/installation")
    def installation_patch(request: Request, body: InstallationPatch, user: User):
        lyst = L(request)
        c = lyst.credentials
        if body.settings:
            for k, v in body.settings.items():
                if k not in ("LYST_LLM_TIER", "LYST_LLM_MODEL", "LYST_LLM_MAX_TIER", "LYST_EMBED_MODEL"):
                    raise ValueError(f"{k} is not an installation setting")
                if k in ("LYST_LLM_TIER", "LYST_LLM_MAX_TIER") and v not in (None, "", "economy", "balanced", "frontier"):
                    raise ValueError("tier must be economy, balanced or frontier")
                if k == "LYST_LLM_MODEL" and v == "auto":
                    v = None
                c.set_setting(user, k, v)
        if body.policy:
            c.set_policy(user, body.policy.provider, workspace_override=body.policy.workspace_override,
                         user_credentials=body.policy.user_credentials)
        if body.add_admin:
            c.add_installation_admin(user, body.add_admin)
        return {"policy": c.policy(), "settings": c.settings_all(), "admins": c.installation_admins()}

    # -- data -------------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/datasets/{name}/row-policies")
    def row_policies(request: Request, ws: str, name: str, user: User):
        return L(request).data.row_policies(user, ws, name)

    @app.post("/v1/workspaces/{ws}/datasets/{name}/row-policies")
    def set_row_policy(request: Request, ws: str, name: str, body: RowPolicyIn, user: User):
        return L(request).data.set_row_policy(user, ws, name, body.column, principal_kind=body.principal_kind, principal=body.principal,
                                              allowed_values=body.allowed_values)

    @app.delete("/v1/workspaces/{ws}/datasets/{name}/row-policies")
    def delete_row_policy(request: Request, ws: str, name: str, user: User, column: str, principal_kind: str, principal: str):
        return {"deleted": L(request).data.delete_row_policy(user, ws, name, column, principal_kind=principal_kind, principal=principal)}

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

    # -- connections ------------------------------------------------------------------------

    @app.get("/v1/connectors")
    def connectors(request: Request, user: User):
        return manifests()

    @app.get("/v1/workspaces/{ws}/connections")
    def connections(request: Request, ws: str, user: User):
        return L(request).connections.list(user, ws)

    @app.post("/v1/workspaces/{ws}/connections")
    def create_connection(request: Request, ws: str, body: ConnectionIn, user: User):
        return L(request).connections.create(user, ws, kind=body.kind, name=body.name, values=body.values)

    @app.post("/v1/workspaces/{ws}/connections/{cid}/test")
    def test_connection(request: Request, ws: str, cid: str, user: User):
        return L(request).connections.test(user, ws, cid)

    @app.delete("/v1/workspaces/{ws}/connections/{cid}")
    def delete_connection(request: Request, ws: str, cid: str, user: User):
        L(request).connections.delete(user, ws, cid)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/connector-drafts")
    def connector_drafts(request: Request, ws: str, user: User):
        return L(request).connections.drafts.list(user, ws)

    @app.post("/v1/workspaces/{ws}/connector-drafts")
    def create_connector_drafts(request: Request, ws: str, body: DraftIn, user: User):
        return L(request).connections.drafts.create(user, ws, name=body.name, openapi=body.openapi, base_url=body.base_url)

    @app.get("/v1/workspaces/{ws}/connector-drafts/{draft_id}")
    def connector_draft(request: Request, ws: str, draft_id: str, user: User):
        return L(request).connections.drafts.get(user, ws, draft_id)

    @app.patch("/v1/workspaces/{ws}/connector-drafts/{draft_id}")
    def edit_connector_draft(request: Request, ws: str, draft_id: str, body: DraftEdit, user: User):
        return L(request).connections.drafts.edit(user, ws, draft_id, body.config, body.cursor_column)

    @app.post("/v1/workspaces/{ws}/connector-drafts/{draft_id}/test")
    def test_connector_draft(request: Request, ws: str, draft_id: str, body: DraftToken, user: User):
        return L(request).connections.drafts.test(user, ws, draft_id, token=body.token)

    @app.post("/v1/workspaces/{ws}/connector-drafts/{draft_id}/promote")
    def promote_connector_draft(request: Request, ws: str, draft_id: str, body: DraftToken, user: User):
        return L(request).connections.drafts.promote(user, ws, draft_id, token=body.token, note=body.note)

    @app.post("/v1/workspaces/{ws}/connector-drafts/{draft_id}/reject")
    def reject_connector_draft(request: Request, ws: str, draft_id: str, body: DraftToken, user: User):
        return L(request).connections.drafts.reject(user, ws, draft_id, note=body.note)

    @app.get("/v1/workspaces/{ws}/connections/health")
    def connections_health(request: Request, ws: str, user: User):
        return L(request).connections.health(user, ws)

    @app.get("/v1/workspaces/{ws}/connections/{cid}/health")
    def connection_health(request: Request, ws: str, cid: str, user: User):
        return L(request).connections.health(user, ws, cid)[0]

    @app.get("/v1/workspaces/{ws}/connections/{cid}/events")
    def connection_events(request: Request, ws: str, cid: str, user: User, limit: int = 50):
        return L(request).connections.events(user, ws, cid, limit=min(max(limit, 1), 500))

    @app.get("/v1/workspaces/{ws}/connections/{cid}/tables")
    def discover(request: Request, ws: str, cid: str, user: User):
        return L(request).connections.discover(user, ws, cid)

    @app.get("/v1/workspaces/{ws}/connections/{cid}/syncs")
    def syncs(request: Request, ws: str, cid: str, user: User):
        return L(request).connections.syncs(user, ws, cid)

    @app.post("/v1/workspaces/{ws}/connections/{cid}/sync")
    def sync_now(request: Request, ws: str, cid: str, body: SyncIn, user: User):
        lyst = L(request)
        r = lyst.connections.sync(user, ws, cid, source_table=body.source_table, dataset=body.dataset, cursor_column=body.cursor_column)
        out = r.__dict__
        if body.every_seconds:
            q = f"sync connection {cid} table {body.source_table}" + (f" by {body.cursor_column}" if body.cursor_column else "") \
                + (f" into {body.dataset}" if body.dataset else "")
            out["schedule"] = lyst.runtime.create_schedule(user, ws, f"Sync {body.source_table}", q, body.every_seconds)
        return out

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

    @app.get("/v1/workspaces/{ws}/metrics/{name}/lineage")
    def metric_lineage(request: Request, ws: str, name: str, user: User):
        return L(request).catalog.explain_metric(user, ws, name)

    @app.get("/v1/workspaces/{ws}/metrics/{name}/history")
    def metric_history(request: Request, ws: str, name: str, user: User):
        return L(request).semantic.history(user, ws, name)

    @app.post("/v1/workspaces/{ws}/metrics/{name}/deprecate")
    def deprecate_metric(request: Request, ws: str, name: str, body: DeprecateIn, user: User):
        return L(request).semantic.deprecate(user, ws, name, replaced_by=body.replaced_by, note=body.note)

    @app.get("/v1/workspaces/{ws}/source-authority")
    def source_authority(request: Request, ws: str, user: User):
        return L(request).semantic.authority(user, ws)

    @app.put("/v1/workspaces/{ws}/datasets/{name}/authority")
    def set_source_authority(request: Request, ws: str, name: str, body: AuthorityIn, user: User):
        return L(request).semantic.set_authority(user, ws, name, body.level, subject=body.subject, note=body.note)

    @app.get("/v1/workspaces/{ws}/semantic/audit")
    def semantic_audit(request: Request, ws: str, user: User):
        return L(request).semantic.audit(user, ws)

    # -- catalog and contracts ----------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/catalog")
    def catalog_overview(request: Request, ws: str, user: User):
        return L(request).catalog.overview(user, ws)

    @app.post("/v1/workspaces/{ws}/catalog/discover")
    def catalog_discover(request: Request, ws: str, user: User):
        return L(request).catalog.discover(user, ws)

    @app.get("/v1/workspaces/{ws}/catalog/relationships")
    def catalog_relationships(request: Request, ws: str, user: User):
        return L(request).catalog.relationships(user, ws)

    @app.get("/v1/workspaces/{ws}/catalog/search")
    def catalog_search(request: Request, ws: str, q: str, user: User, limit: int = 20):
        return L(request).catalog.search(user, ws, q, limit=min(max(limit, 1), 100))

    @app.get("/v1/workspaces/{ws}/datasets/{name}/insights")
    def dataset_insights(request: Request, ws: str, name: str, user: User):
        return L(request).catalog.insights(user, ws, name)

    @app.get("/v1/workspaces/{ws}/datasets/{name}/impact")
    def dataset_impact(request: Request, ws: str, name: str, user: User, columns: str | None = None):
        return L(request).catalog.impact(user, ws, name, [c for c in (columns or "").split(",") if c] or None)

    @app.get("/v1/workspaces/{ws}/datasets/{name}/contract")
    def get_contract(request: Request, ws: str, name: str, user: User):
        return L(request).contracts.get(user, ws, name) or {"dataset": name, "active": None, "versions": [], "status": None}

    @app.get("/v1/workspaces/{ws}/datasets/{name}/contract/draft")
    def draft_contract(request: Request, ws: str, name: str, user: User):
        return L(request).contracts.draft(user, ws, name)

    @app.post("/v1/workspaces/{ws}/datasets/{name}/contract")
    def define_contract(request: Request, ws: str, name: str, body: ContractIn, user: User):
        return L(request).contracts.define(user, ws, name, body.spec, note=body.note)

    @app.delete("/v1/workspaces/{ws}/datasets/{name}/contract")
    def retire_contract(request: Request, ws: str, name: str, user: User):
        L(request).contracts.retire(user, ws, name)
        return {"ok": True}

    @app.post("/v1/workspaces/{ws}/datasets/{name}/contract/check")
    def check_contract(request: Request, ws: str, name: str, user: User):
        return L(request).contracts.check(user, ws, name)

    @app.get("/v1/workspaces/{ws}/datasets/{name}/contract/checks")
    def contract_checks(request: Request, ws: str, name: str, user: User):
        return L(request).contracts.checks(user, ws, name)

    # -- asset registry -----------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/assets")
    def assets(request: Request, ws: str, user: User, kind: str | None = None, health: str | None = None, retired: bool = False):
        return L(request).assets.list(user, ws, kind=kind, health=health, include_retired=retired)

    @app.post("/v1/workspaces/{ws}/assets/scan")
    def scan_assets(request: Request, ws: str, user: User):
        return L(request).assets.scan_now(user, ws)

    @app.get("/v1/workspaces/{ws}/assets/{asset_id}")
    def asset(request: Request, ws: str, asset_id: str, user: User):
        return L(request).assets.get(user, ws, asset_id)

    @app.get("/v1/workspaces/{ws}/assets/{asset_id}/proposal")
    def asset_proposal(request: Request, ws: str, asset_id: str, user: User):
        return L(request).assets.propose(user, ws, asset_id)

    @app.post("/v1/workspaces/{ws}/assets/{asset_id}/repair")
    def repair_asset(request: Request, ws: str, asset_id: str, user: User):
        return L(request).assets.repair(user, ws, asset_id)

    @app.post("/v1/workspaces/{ws}/assets/{asset_id}/retire")
    def retire_asset(request: Request, ws: str, asset_id: str, user: User):
        return L(request).assets.retire(user, ws, asset_id)

    # -- transformations ------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/transforms")
    def transforms(request: Request, ws: str, user: User):
        return L(request).transforms.list(user, ws)

    @app.post("/v1/workspaces/{ws}/transforms/design")
    def design_transform(request: Request, ws: str, body: DesignIn, user: User):
        return L(request).transforms.design(user, ws, body.request)

    @app.post("/v1/workspaces/{ws}/transforms/preview")
    def preview_transform(request: Request, ws: str, body: PreviewIn, user: User):
        return L(request).transforms.preview(user, ws, body.sql, key=body.key)

    @app.post("/v1/workspaces/{ws}/transforms")
    def define_transform(request: Request, ws: str, body: TransformIn, user: User):
        return L(request).transforms.define(user, ws, body.name, body.sql, title=body.title, description=body.description, key=body.key,
                                            incremental_column=body.incremental_column, note=body.note)

    @app.get("/v1/workspaces/{ws}/transforms/{name}")
    def get_transform(request: Request, ws: str, name: str, user: User):
        return L(request).transforms.get(user, ws, name)

    @app.post("/v1/workspaces/{ws}/transforms/{name}/run")
    def run_transform(request: Request, ws: str, name: str, user: User, full: bool = False):
        return L(request).transforms.run(user, ws, name, full=full)

    @app.post("/v1/workspaces/{ws}/transforms/{name}/retire")
    def retire_transform(request: Request, ws: str, name: str, user: User):
        return L(request).transforms.retire(user, ws, name)

    # -- collaboration --------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/objects/{kind}/{oid}/activity")
    def activity(request: Request, ws: str, kind: str, oid: str, user: User):
        return L(request).collab.activity(user, ws, kind, oid)

    @app.get("/v1/workspaces/{ws}/objects/{kind}/{oid}/comments")
    def list_comments(request: Request, ws: str, kind: str, oid: str, user: User):
        return L(request).collab.comments(user, ws, kind, oid)

    @app.post("/v1/workspaces/{ws}/objects/{kind}/{oid}/comments")
    def add_comment(request: Request, ws: str, kind: str, oid: str, body: CommentIn, user: User):
        return L(request).collab.comment(user, ws, kind, oid, body.body, kind=body.kind, parent_id=body.parent_id)

    @app.post("/v1/workspaces/{ws}/comments/{comment_id}/resolve")
    def resolve_comment(request: Request, ws: str, comment_id: str, body: ResolveIn, user: User):
        return L(request).collab.resolve(user, ws, comment_id, note=body.note)

    @app.get("/v1/workspaces/{ws}/decisions")
    def decisions(request: Request, ws: str, user: User, object_kind: str | None = None, object_id: str | None = None):
        return L(request).collab.decisions(user, ws, object_kind=object_kind, object_id=object_id)

    @app.get("/v1/workspaces/{ws}/assignments")
    def assignments(request: Request, ws: str, user: User, mine: bool = False, status: str | None = None):
        return L(request).collab.assignments(user, ws, mine=mine, status=status)

    @app.post("/v1/workspaces/{ws}/assignments")
    def assign(request: Request, ws: str, body: AssignIn, user: User):
        return L(request).collab.assign(user, ws, body.object_kind, body.object_id, body.assignee, note=body.note, due=body.due)

    @app.post("/v1/workspaces/{ws}/assignments/{assignment_id}/complete")
    def complete_assignment(request: Request, ws: str, assignment_id: str, body: CompleteIn, user: User):
        return L(request).collab.complete(user, ws, assignment_id, note=body.note)

    @app.get("/v1/workspaces/{ws}/reviews")
    def reviews(request: Request, ws: str, user: User, status: str | None = None, for_me: bool = False):
        return L(request).collab.reviews(user, ws, status=status, for_me=for_me)

    @app.post("/v1/workspaces/{ws}/reviews")
    def propose(request: Request, ws: str, body: ProposeIn, user: User):
        return L(request).collab.propose(user, ws, body.action, object_kind=body.object_kind, object_id=body.object_id,
                                         payload=body.payload, summary=body.summary, reviewer=body.reviewer)

    @app.post("/v1/workspaces/{ws}/reviews/{review_id}/decide")
    def decide_review(request: Request, ws: str, review_id: str, body: DecideIn, user: User):
        return L(request).collab.decide(user, ws, review_id, approve=body.approve, note=body.note)

    @app.post("/v1/workspaces/{ws}/assets/{asset_id}/share")
    def share_asset(request: Request, ws: str, asset_id: str, body: ShareIn, user: User):
        return L(request).collab.share(user, ws, asset_id, visibility=body.visibility, users=body.users)

    @app.post("/v1/workspaces/{ws}/ownership")
    def transfer_ownership(request: Request, ws: str, body: TransferIn, user: User):
        return L(request).collab.transfer(user, ws, body.object_kind, body.object_id, body.to_user, note=body.note)

    @app.get("/v1/workspaces/{ws}/notifications")
    def my_notifications(request: Request, ws: str, user: User, unread: bool = False):
        return L(request).collab.notifications(user, ws, unread_only=unread)

    @app.post("/v1/workspaces/{ws}/notifications/{notification_id}/read")
    def read_notification(request: Request, ws: str, notification_id: str, user: User):
        L(request).collab.mark_read(user, ws, notification_id)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/notification-preferences")
    def notification_prefs(request: Request, ws: str, user: User):
        return L(request).collab.preferences(user, ws)

    @app.put("/v1/workspaces/{ws}/notification-preferences")
    def set_notification_prefs(request: Request, ws: str, body: dict[str, bool], user: User):
        return L(request).collab.set_preferences(user, ws, body)

    # -- analyst and work -------------------------------------------------------------------

    @app.post("/v1/workspaces/{ws}/tasks", status_code=202)
    def ask(request: Request, ws: str, body: Ask, user: User):
        # Idempotency-Key: a retried or double-submitted request returns the first task instead of a second run
        return {"task_id": L(request).runtime.submit(user, ws, body.question, autonomy=body.autonomy,
                                                     idempotency_key=request.headers.get("idempotency-key"))}

    @app.get("/v1/workspaces/{ws}/workflows")
    def workflows(request: Request, ws: str, user: User, history: bool = False):
        return L(request).services.workflows.list(user, ws, include_history=history)

    @app.get("/v1/workspaces/{ws}/workflows/{wf_id}")
    def workflow(request: Request, ws: str, wf_id: str, user: User):
        return L(request).services.workflows.get(user, ws, wf_id)

    @app.post("/v1/workspaces/{ws}/workflows/{wf_id}/run", status_code=202)
    def run_workflow(request: Request, ws: str, wf_id: str, body: RunWorkflow, user: User):
        lyst = L(request)
        return {"task_id": lyst.services.workflows.run(user, ws, wf_id, lyst.runtime, period=body.period)}

    @app.post("/v1/workspaces/{ws}/workflows/{wf_id}/schedule")
    def schedule_workflow(request: Request, ws: str, wf_id: str, body: ScheduleWorkflow, user: User):
        lyst = L(request)
        return lyst.services.workflows.schedule(user, ws, wf_id, lyst.runtime, body.every_seconds)

    @app.post("/v1/workspaces/{ws}/workflows/{wf_id}/retire")
    def retire_workflow(request: Request, ws: str, wf_id: str, user: User):
        L(request).services.workflows.retire(user, ws, wf_id)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/tasks")
    def tasks(request: Request, ws: str, user: User):
        return L(request).runtime.list(user, ws)

    @app.get("/v1/tasks/{task_id}")
    def task(request: Request, task_id: str, user: User):
        return L(request).runtime.get(user, task_id)

    @app.get("/v1/tasks/{task_id}/trace")
    def task_trace(request: Request, task_id: str, user: User):
        return L(request).runtime.trace(user, task_id)

    @app.post("/v1/tasks/{task_id}/follow-up", status_code=202)
    def follow_up(request: Request, task_id: str, body: FollowUpIn, user: User):
        """Apply a follow-up ("break that down by region", "compare it to last year", "make this a dashboard",
        "schedule this every Monday") to an earlier answer; the new task records what it came from and what changed."""
        lyst = L(request)
        return FollowUps(lyst.runtime, lyst.semantic).apply(user, task_id, body.text, timezone=body.timezone)

    @app.get("/v1/workspaces/{ws}/memory/review")
    def memory_review(request: Request, ws: str, user: User):
        """Memories that may no longer apply (newer data, a changed definition, or age), with the reason (admins)."""
        return L(request).memory.review(user, ws)

    @app.get("/v1/workspaces/{ws}/skills/discover")
    def discover_skills(request: Request, ws: str, q: str, user: User):
        lyst = L(request)
        return lyst.skills.discover(user, ws, q, lyst.services.registry)

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
        return L(request).runtime.create_schedule(user, ws, body.title, body.question, body.every_seconds, at=body.at,
                                                  weekdays=body.weekdays, timezone=body.timezone, overlap=body.overlap)

    # -- scenarios and decisions ------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/scenario-models")
    def scenario_models(request: Request, ws: str, user: User):
        return L(request).services.scenarios.models(user, ws)

    @app.post("/v1/workspaces/{ws}/scenario-models")
    def define_scenario_model(request: Request, ws: str, body: ScenarioModelIn, user: User):
        return L(request).services.scenarios.define(user, ws, body.name, body.spec, note=body.note)

    @app.get("/v1/workspaces/{ws}/metrics/{metric}/scenario-models")
    def scenario_models_for_metric(request: Request, ws: str, metric: str, user: User):
        return L(request).services.scenarios.for_metric(user, ws, metric)

    @app.get("/v1/workspaces/{ws}/scenario-models/{name}")
    def scenario_model(request: Request, ws: str, name: str, user: User, version: int | None = None):
        return L(request).services.scenarios.get(user, ws, name, version)

    @app.post("/v1/workspaces/{ws}/scenario-models/{name}/evaluate")
    def evaluate_scenario(request: Request, ws: str, name: str, body: ScenarioRun, user: User):
        return L(request).services.scenarios.evaluate(user, ws, name, body.overrides, version=body.version,
                                                      previous=body.previous)

    @app.post("/v1/workspaces/{ws}/scenario-models/{name}/analyze")
    def analyze_scenario(request: Request, ws: str, name: str, body: ScenarioAnalysisIn, user: User):
        return L(request).services.scenarios.analyze(user, ws, name, body.overrides, output=body.output, draws=body.draws,
                                                     seed=body.seed, thresholds=body.thresholds)

    @app.get("/v1/workspaces/{ws}/scenario-models/{name}/scenarios")
    def saved_scenarios(request: Request, ws: str, name: str, user: User):
        return L(request).services.scenarios.saved(user, ws, name)

    @app.post("/v1/workspaces/{ws}/scenario-models/{name}/scenarios")
    def save_scenario(request: Request, ws: str, name: str, body: ScenarioSave, user: User):
        return L(request).services.scenarios.save(user, ws, name, body.name, body.overrides, note=body.note)

    @app.get("/v1/workspaces/{ws}/scenarios/compare")
    def compare_scenarios(request: Request, ws: str, ids: str, user: User):
        return L(request).services.scenarios.compare_saved(user, ws, [i for i in ids.split(",") if i])

    @app.post("/v1/workspaces/{ws}/scenarios/{scenario_id}/reproduce")
    def reproduce_scenario(request: Request, ws: str, scenario_id: str, user: User):
        return L(request).services.scenarios.reproduce(user, ws, scenario_id)

    @app.post("/v1/workspaces/{ws}/what-if")
    def what_if(request: Request, ws: str, body: WhatIf, user: User):
        return L(request).services.scenarios.ask(user, ws, body.question, body.model)

    @app.post("/v1/workspaces/{ws}/decision-records/frame")
    def frame_decision(request: Request, ws: str, body: FrameIn, user: User):
        L(request).services.guard.require(user, ws, "workspace.read")
        return decision_mod.frame(body.question)

    @app.post("/v1/workspaces/{ws}/decision-records/allocate")
    def allocate(request: Request, ws: str, body: AllocateIn, user: User):
        L(request).services.guard.require(user, ws, "workspace.read")
        return decision_mod.allocate(body.items, body.budget, groups=body.groups, integer=body.integer)

    @app.get("/v1/workspaces/{ws}/decision-records")
    def list_decisions(request: Request, ws: str, user: User):
        return L(request).services.decisions.list(user, ws)

    @app.post("/v1/workspaces/{ws}/decision-records")
    def create_decision(request: Request, ws: str, body: DecisionIn, user: User):
        lyst = L(request)
        if body.task_id:
            lyst.runtime.get(user, body.task_id)  # the person must be able to read the task they link
        return lyst.services.decisions.create(user, ws, body.title, body.spec, task_id=body.task_id)

    @app.get("/v1/workspaces/{ws}/decision-records/{decision_id}")
    def get_decision(request: Request, ws: str, decision_id: str, user: User):
        return L(request).services.decisions.get(user, ws, decision_id)

    @app.post("/v1/workspaces/{ws}/decision-records/{decision_id}/submit")
    def submit_decision(request: Request, ws: str, decision_id: str, body: DecisionSubmit, user: User):
        return L(request).services.decisions.submit(user, ws, decision_id, body.chosen, body.rationale)

    @app.post("/v1/workspaces/{ws}/decision-records/{decision_id}/review")
    def review_decision(request: Request, ws: str, decision_id: str, body: DecisionReview, user: User):
        return L(request).services.decisions.review(user, ws, decision_id, body.observed, body.note)

    # -- operations -------------------------------------------------------------------------

    @app.get("/v1/operations")
    def operations_all(request: Request, user: User):
        """Installation-wide operations: queues by priority, running work per workspace, supervisor findings, failures,
        run times and model use against budgets. Installation administrators only."""
        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("operations across workspaces are for installation administrators")
        return operations.report(lyst.runtime)

    @app.get("/v1/workspaces/{ws}/operations")
    def operations_ws(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.services.guard.require(user, ws, "workspace.admin")
        return operations.report(lyst.runtime, ws)

    @app.put("/v1/workspaces/{ws}/quotas")
    def put_quotas(request: Request, ws: str, body: QuotasIn, user: User):
        """Quotas are set by installation administrators: a workspace admin cannot raise their own share."""
        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("workspace quotas are set by installation administrators")
        out = operations.set_quotas(lyst.runtime.db, ws, user.user_id, body.model_dump(exclude_unset=True))
        lyst.services.guard.audit(user, ws, "quota.set", ws, "ok", body.model_dump(exclude_unset=True))
        return out

    # -- proactive --------------------------------------------------------------------------

    @app.get("/v1/workspaces/{ws}/pulse")
    def pulse(request: Request, ws: str, user: User):
        """The approved metrics' last complete month against the one before (only months: the history drops an
        incomplete final month, which a quarter would not get)."""
        return L(request).monitors.pulse(user, ws)

    @app.get("/v1/workspaces/{ws}/monitors")
    def monitors(request: Request, ws: str, user: User):
        return L(request).monitors.list(user, ws)

    @app.post("/v1/workspaces/{ws}/monitors")
    def create_monitor(request: Request, ws: str, body: MonitorIn, user: User):
        return L(request).monitors.create(user, ws, **body.model_dump())

    @app.post("/v1/workspaces/{ws}/monitors/{monitor_id}/disable")
    def disable_monitor(request: Request, ws: str, monitor_id: str, user: User):
        L(request).monitors.disable(user, ws, monitor_id)
        return {"ok": True}

    @app.post("/v1/workspaces/{ws}/monitors/tick")
    def tick_monitors(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "task.control")
        return lyst.monitors.tick(ws)

    @app.get("/v1/workspaces/{ws}/findings")
    def findings(request: Request, ws: str, user: User, status: str | None = None):
        return L(request).monitors.findings(user, ws, status=status)

    @app.post("/v1/workspaces/{ws}/findings/{finding_id}")
    def set_finding(request: Request, ws: str, finding_id: str, body: FindingStatus, user: User):
        L(request).monitors.set_status(user, ws, finding_id, body.status)
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/inbox")
    def inbox(request: Request, ws: str, user: User):
        return L(request).monitors.inbox(user, ws)

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

    @app.post("/v1/workspaces/{ws}/documents/reindex")
    def reindex_documents(request: Request, ws: str, user: User):
        return L(request).knowledge.reindex(user, ws)

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

    @app.get("/v1/workspaces/{ws}/dashboards")
    def dashboards(request: Request, ws: str, user: User, include_retired: bool = False):
        return L(request).studio.list(user, ws, include_retired=include_retired)

    @app.get("/v1/workspaces/{ws}/dashboards/{dashboard_id}")
    def dashboard_manifest(request: Request, ws: str, dashboard_id: str, user: User):
        return L(request).studio.manifest(user, ws, dashboard_id)

    @app.get("/v1/workspaces/{ws}/dashboards/{dashboard_id}/bundle")
    def dashboard_bundle(request: Request, ws: str, dashboard_id: str, user: User):
        data = L(request).studio.bundle(user, ws, dashboard_id)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{dashboard_id}.zip"'})

    @app.get("/v1/workspaces/{ws}/dashboards/{dashboard_id}/files/{filename}")
    def dashboard_file(request: Request, ws: str, dashboard_id: str, filename: str, user: User):
        return Response(L(request).studio.extract(user, ws, dashboard_id, filename), media_type="application/octet-stream")

    @app.post("/v1/workspaces/{ws}/dashboards/{dashboard_id}/refresh")
    def dashboard_refresh(request: Request, ws: str, dashboard_id: str, user: User):
        return L(request).studio.refresh(user, ws, dashboard_id)

    @app.post("/v1/workspaces/{ws}/dashboards/{dashboard_id}/retire")
    def dashboard_retire(request: Request, ws: str, dashboard_id: str, user: User):
        return L(request).studio.retire(user, ws, dashboard_id)

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
        from ..deliverables import KINDS  # noqa: PLC0415

        meta, data = L(request).artifacts.get(user, artifact_id)
        name, mime = KINDS[meta["kind"]]
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
        info = lyst.llm.info(ws)
        calls = lyst.stores.control.query(
            "SELECT count(*) AS calls, coalesce(sum(input_tokens), 0) AS input_tokens, "
            "coalesce(sum(output_tokens), 0) AS output_tokens FROM llm_calls WHERE workspace_id = ?", [ws])[0]
        credentials = None
        if role == "owner":
            c = lyst.credentials.resolve("openrouter", workspace=ws) if lyst.settings.llm.provider == "openrouter" else None
            credentials = {"model_credential": {"scope": c["scope_kind"], "name": c["name"], "id": c["id"]} if c else None,
                           "installation_admin": lyst.credentials.is_installation_admin(user),
                           "policy": lyst.credentials.policy().get("openrouter")}

        return {"workspace": w, "role": role, "model": info.__dict__ if info else None, "llm_usage": calls,
                "gateway": lyst.llm.status() if role in ("admin", "owner") else None,
                "credentials": credentials, "model_ready": lyst.llm.allowed_for(ws), "provider": lyst.settings.llm.provider,
                "encrypted": lyst.settings.encrypt, "home": str(lyst.settings.home) if role == "owner" else None}

    @app.patch("/v1/workspaces/{ws}/settings")
    def patch_settings(request: Request, ws: str, body: WorkspacePatch, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.admin")
        if body.autonomy is not None and body.autonomy not in ("assist", "delegate", "autonomous", "governed"):
            raise ValueError("unknown autonomy mode")
        if body.webhook_url and not body.webhook_url.startswith("https://"):
            raise ValueError("the webhook must be an https URL")
        updates: list[tuple[str, Any]] = []
        if body.allow_external_llm is not None:
            updates.append(("allow_external_llm", body.allow_external_llm))
        if body.autonomy is not None:
            updates.append(("autonomy", body.autonomy))
        if body.webhook_url is not None:  # "" clears it
            updates.append(("webhook_url", body.webhook_url or None))
        if body.llm_tier is not None or body.llm_model is not None:
            from ..llm import policy  # noqa: PLC0415

            row = lyst.stores.control.query("SELECT llm_tier, llm_model FROM workspaces WHERE id = ?", [ws])[0]
            tier = row["llm_tier"] if body.llm_tier is None else (body.llm_tier or None)
            model = row["llm_model"] if body.llm_model is None else (None if body.llm_model in ("", "auto") else body.llm_model.strip())
            if tier not in (None, "economy", "balanced", "frontier"):
                raise ValueError("tier must be economy, balanced or frontier")
            sel = lyst.llm.selection(ws)
            reason = policy.choice_allowed(tier, model, sel.ceiling if sel else "frontier")
            if reason:
                raise AccessDenied(reason)
            updates += [("llm_tier", tier), ("llm_model", model)]
        for col, val in updates:
            lyst.stores.control.execute(f"UPDATE workspaces SET {col} = ? WHERE id = ?", [val, ws])
            lyst.guard.audit(user, ws, f"settings.{col}", ws, "ok", {"value": val})
        return {"ok": True}

    @app.get("/v1/workspaces/{ws}/model-options")
    def model_options(request: Request, ws: str, user: User):
        """The tiers and models this workspace may choose, priced from OpenRouter's catalog, and what it uses now."""
        from ..llm.options import model_options as options  # noqa: PLC0415

        lyst = L(request)
        lyst.guard.require(user, ws, "workspace.read")
        if lyst.llm.catalog is not None:
            lyst.llm.catalog.refresh_if_stale()
        return options(lyst.llm, lyst.stores, ws)

    @app.post("/v1/models/catalog/refresh")
    def model_catalog_refresh(request: Request, user: User):
        """Rebuild the tiers from OpenRouter's live model list now (installation administrators)."""
        lyst = L(request)
        if not lyst.credentials.is_installation_admin(user):
            raise AccessDenied("the model catalog is refreshed by installation administrators")
        if lyst.llm.catalog is None:
            raise ValueError("the model catalog applies to OpenRouter; this installation uses a local model")
        out = lyst.llm.catalog.refresh(actor=user.user_id)
        lyst.credentials._event(user, "models.catalog_refresh", None, "installation", None, "ok",  # noqa: SLF001
                                {"models": out["modelCount"], "changes": out["changes"][:20]})
        return out

    # -- anticipation (anticipate/): activity center, explanations, responses, autonomy level and budgets --
    from .anticipate import routes as anticipation_routes  # noqa: PLC0415

    anticipation_routes(app, L, User)

    @app.get("/v1/workspaces/{ws}/model-usage")
    def model_usage(request: Request, ws: str, user: User):
        lyst = L(request)
        lyst.guard.require(user, ws, "audit.read")
        return lyst.stores.control.query(
            "SELECT coalesce(tier, '') AS tier, coalesce(workload, purpose) AS workload, model, provider, count(*) AS calls, "
            "count(*) FILTER (WHERE outcome = 'ok') AS ok, coalesce(sum(input_tokens), 0) AS input_tokens, "
            "coalesce(sum(output_tokens), 0) AS output_tokens, round(coalesce(sum(cost_usd), 0), 6) AS cost_usd, "
            "quantile_cont(latency_ms, 0.95) AS p95_ms FROM llm_calls WHERE workspace_id = ? GROUP BY ALL ORDER BY calls DESC",
            [ws])

    @app.get("/v1/workspaces/{ws}/model-registry")
    def model_registry(request: Request, ws: str, user: User):
        """What each model offers (context, prices, reasoning settings, tier) beside how it has measured up here (success
        rate, latency, tokens and cost per workload over 7 days), and the workspace's inference policy."""
        lyst = L(request)
        lyst.guard.require(user, ws, "audit.read")
        from ..llm import catalog as model_catalog  # noqa: PLC0415
        from ..llm.scorecard import scorecard  # noqa: PLC0415

        measured: dict[str, list] = {}
        for row in scorecard(lyst.stores.control, workspace_id=ws):
            measured.setdefault(row["model"], []).append({k: v for k, v in row.items() if k != "model"})
        settings = lyst.settings.llm
        models = []
        if settings.provider == "openrouter":
            release = model_catalog.active()
            tiers_of: dict[str, list[str]] = {}
            for tier, items in release["tiers"].items():
                for m in items:
                    tiers_of.setdefault(m["modelId"], []).append(tier)
            seen = set()
            for _tier, items in release["tiers"].items():
                for m in items:
                    if m["modelId"] in seen:
                        continue
                    seen.add(m["modelId"])
                    models.append({"model": m["modelId"], "label": m.get("modelLabel"), "vendor": m.get("vendor"),
                                   "tiers": tiers_of[m["modelId"]], "context": m.get("context"),
                                   "usd_per_million_input": round(m.get("inputUsdPerToken", 0) * 1e6, 4),
                                   "usd_per_million_output": round(m.get("outputUsdPerToken", 0) * 1e6, 4),
                                   "reasoning": m.get("reasoningInfo"), "local": False,
                                   "measured": measured.pop(m["modelId"], [])})
        elif settings.model:
            models.append({"model": settings.model, "label": settings.model, "vendor": None, "tiers": [], "context": None,
                           "local": not settings.external, "measured": measured.pop(settings.model, [])})
        for mid, rows in measured.items():  # models used here that the current catalog no longer lists
            models.append({"model": mid, "label": mid, "tiers": [], "listed": False, "measured": rows})
        ws_row = lyst.stores.control.query("SELECT allow_external_llm, llm_tier, llm_model FROM workspaces WHERE id = ?", [ws])
        return {"provider": settings.provider or ("local" if settings.model else None), "external": settings.external,
                "policy": ws_row[0] if ws_row else None, "routing": "tier order; models failing a workload recently "
                "move behind healthy ones (pinned models are never reordered)", "models": models}


    from .adapt import register as register_adapt  # noqa: PLC0415

    register_adapt(app, L, User)

    # -- extensibility lane: ingestion, SDK contracts, experiments, environments, export/import, publishing ---------
    from .extend import register as register_extend  # noqa: PLC0415

    register_extend(app, L, User)

    # -- specializations lane: the Universal Analyst Core entry point and the Specializations catalog --
    from .specialists import register as register_specialists  # noqa: PLC0415

    register_specialists(app, L, User)
    # -- voice (voice/): settings, engines, sessions, turns, read-back, duplex stream --------------------------------
    from .voice import routes as voice_routes  # noqa: PLC0415

    voice_routes(app, L, User)
    # -- UI lane: each person's pins --------------------------------------------------------------------------------
    from .ui import routes as ui_routes  # noqa: PLC0415

    ui_routes(app, L, User)

    return app


def main() -> None:  # pragma: no cover - process entry
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("LYST_HOST", "127.0.0.1"),
                port=int(os.environ.get("LYST_PORT", "8770")))
