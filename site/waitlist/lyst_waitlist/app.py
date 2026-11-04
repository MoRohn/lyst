"""HTTP API: POST /v1/registrations and GET /healthz. Nothing else is exposed."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sys
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .ratelimit import RateLimiter
from .store import CONSENT_SCOPES, Registration, Store

EMAIL_RE = re.compile(r"^[^\s@<>()\[\],;:\"]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,63}$")
ROLES = {
    "Business analyst", "Analytics engineer", "Data scientist", "Finance or FP&A", "Operations",
    "Manager or leader", "Engineer or developer", "Other",
}
INTERESTS = {
    "Financial analysis", "Operational analytics", "Dashboards and reporting", "Data pipelines",
    "Self-hosted AI", "Something else",
}
TEAM_SIZES = {"1–5", "6–20", "21–100", "100+"}
OK_MESSAGE = "Thank you. You’re on the early-access list. We’ll be in touch with previews and release updates."
PARTNER_MESSAGE = "Thank you. Your design partner application was received. We’ll reply by email."


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False)


class _StdoutHandler(logging.StreamHandler):
    """Always writes to the current sys.stdout (works with process managers and test capture)."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


def _logger() -> logging.Logger:
    log = logging.getLogger("lyst_waitlist")
    if not log.handlers:
        h = _StdoutHandler()
        h.setFormatter(JsonFormatter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def _clean(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return value[:max_len] or None


def validate(body: Any) -> tuple[Registration | None, dict[str, str]]:
    """Validate a request body. Returns (registration, errors)."""
    errors: dict[str, str] = {}
    if not isinstance(body, dict):
        return None, {"body": "invalid"}

    kind = body.get("kind", "early_access")
    if kind not in CONSENT_SCOPES:
        errors["kind"] = "invalid"

    email = body.get("email")
    if not isinstance(email, str) or not email.strip():
        errors["email"] = "email_required"
    else:
        email = email.strip()
        if len(email) > 254 or not EMAIL_RE.match(email):
            errors["email"] = "email_invalid"

    if body.get("consent") is not True:
        errors["consent"] = "consent_required"

    consent_version = body.get("consent_version")
    if not isinstance(consent_version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", consent_version):
        errors["consent_version"] = "invalid"

    try:
        role = _clean(body.get("role"), 60)
        organization = _clean(body.get("organization"), 120)
        interest = _clean(body.get("interest"), 60)
        team_size = _clean(body.get("team_size"), 20)
        message = _clean(body.get("message"), 1000)
    except TypeError:
        return None, {**errors, "body": "invalid"}

    if role and role not in ROLES:
        errors["role"] = "invalid"
    if interest and interest not in INTERESTS:
        errors["interest"] = "invalid"
    if team_size and team_size not in TEAM_SIZES:
        errors["team_size"] = "invalid"
    if kind == "design_partner" and not organization:
        errors["organization"] = "organization_required"

    if errors:
        return None, errors
    return (
        Registration(
            kind=kind,
            email=email,
            consent_version=consent_version,
            role=role,
            organization=organization,
            interest=interest,
            team_size=team_size,
            message=message if kind == "design_partner" else None,
        ),
        {},
    )


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    log = _logger()
    store = store or Store(settings.db_path, settings.hash_key, settings.encryption_key)
    limiter = RateLimiter(*settings.rate_limit)

    app = FastAPI(title="Lyst waitlist", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["POST"],
        allow_headers=["Content-Type", "Accept"],
        allow_credentials=False,
        max_age=600,
    )

    def client_key(request: Request) -> str:
        ip = request.client.host if request.client else "unknown"
        if settings.trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                ip = fwd.split(",")[0].strip()
        return hmac.new(settings.hash_key, ip.encode(), hashlib.sha256).hexdigest()[:24]

    def event(name: str, **fields: Any) -> None:
        log.info(name, extra={"fields": {"event": name, **fields}})

    @app.middleware("http")
    async def harden(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"status": "error", "message": "Request too large."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/registrations")
    async def register(request: Request):
        rid = request.state.request_id
        key = client_key(request)
        origin = request.headers.get("origin", "").rstrip("/")

        if origin and origin not in settings.allowed_origins:
            event("rejected", reason="origin", request_id=rid, client=key)
            return JSONResponse({"status": "error", "message": "Origin not allowed."}, status_code=403)

        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"status": "error", "message": "Use application/json."}, status_code=415)

        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            return JSONResponse({"status": "error", "message": "Request too large."}, status_code=413)

        if not limiter.allow(key):
            event("rate_limited", request_id=rid, client=key)
            return JSONResponse(
                {"status": "error", "message": "Too many attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(settings.rate_limit[1])},
            )

        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"status": "error", "errors": {"body": "invalid"}}, status_code=422)

        # Honeypot: bots fill hidden fields. Respond as if accepted, store nothing.
        if isinstance(body, dict) and body.get("website"):
            event("honeypot", request_id=rid, client=key)
            return JSONResponse({"status": "ok", "message": OK_MESSAGE})

        reg, errors = validate(body)
        if reg is None:
            event("invalid", request_id=rid, client=key, fields=sorted(errors))
            return JSONResponse({"status": "error", "errors": errors}, status_code=422)

        reg = Registration(**{**reg.__dict__, "origin": origin or None})
        try:
            created = store.add(reg)
        except Exception:  # noqa: BLE001 - never leak internals
            log.exception("store_failed", extra={"fields": {"event": "store_failed", "request_id": rid}})
            return JSONResponse(
                {"status": "error", "message": "Registration could not be saved. Nothing was stored."},
                status_code=503,
            )

        event("registered" if created else "duplicate", request_id=rid, client=key, kind=reg.kind)
        # Same response for new and existing addresses so the API can't be used to test membership.
        return JSONResponse(
            {"status": "ok", "message": PARTNER_MESSAGE if reg.kind == "design_partner" else OK_MESSAGE}
        )

    return app


def app_factory() -> FastAPI:  # for `uvicorn --factory lyst_waitlist.app:app_factory`
    return create_app()
