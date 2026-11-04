# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Routes of the extensibility lane: multimodal ingestion (and, as they land, experiments, environments, SDK
contracts, export/import and publishing). Registered from ``create_app`` in one block.

No ``from __future__ import annotations`` here: FastAPI resolves the ``User`` dependency from real annotations.
"""

from typing import Annotated, Any

from fastapi import File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

MAX_UPLOAD = 200 * 1024 * 1024


class IngestChoice(BaseModel):
    key: str
    name: str | None = None
    include: bool = True


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
