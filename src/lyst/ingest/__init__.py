# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Multimodal ingestion: one door for files of any kind, with a look before anything is loaded.

``inspect`` reads an uploaded file and returns a plan: what the file is, every table found in it (a workbook's
sheets and the separate tables inside them, the tables inside a Word document, a slide or a web page), each
table's columns with types, units and formulas, its total rows checked against the sum of its rows, and the
document text that becomes searchable. Nothing is loaded yet. ``commit`` loads what the person chose (each table
a new dataset version through the governed data service, the document text into cited knowledge search) and
links the two in lineage. The staged file lives inside the encrypted workspace database and is dropped once the
job is committed or discarded; only its SHA-256 stays.

Supported: CSV/TSV/TXT (encoding and delimiter detected), Parquet, JSON/NDJSON, Excel .xlsx/.xlsm, Word .docx,
PowerPoint .pptx, PDF, HTML, Markdown, plain text. Not supported, and said so: legacy .xls/.doc/.ppt, images and
scanned PDFs (no bundled OCR), encrypted workbooks.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..data import normalise_name
from ..security import Guard, Principal
from ..store import Stores
from .documents import decode, read_document
from .spreadsheet import read_workbook
from .tables import locate_tables, preview, proposed_name, shape_table

log = logging.getLogger("lyst.ingest")
GRID_LIMIT = 20 * 1024 * 1024  # delimited files above this skip cell-level reading and load column-wise
FORMATS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "text", ".parquet": "parquet", ".json": "json", ".jsonl": "ndjson",
    ".ndjson": "ndjson", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls", ".docx": "docx", ".pptx": "pptx",
    ".pdf": "pdf", ".html": "html", ".htm": "html", ".md": "markdown", ".markdown": "markdown",
}
UNSUPPORTED = {
    ".doc": "a Word 97-2003 (.doc) file; save it as .docx",
    ".ppt": "a PowerPoint 97-2003 (.ppt) file; save it as .pptx",
    ".png": "an image; Lyst has no built-in OCR, so add the text or the table it shows as a file",
    ".jpg": "an image; Lyst has no built-in OCR, so add the text or the table it shows as a file",
    ".jpeg": "an image; Lyst has no built-in OCR, so add the text or the table it shows as a file",
    ".gif": "an image; Lyst has no built-in OCR", ".heic": "an image; Lyst has no built-in OCR",
    ".zip": "an archive; upload the files inside it one at a time",
}


def formats() -> dict[str, Any]:
    """What the uploader may offer, so its file picker stays in sync with what is read."""
    groups = {"spreadsheet": ["xlsx", "xlsm"], "table": ["csv", "tsv", "txt", "parquet", "json", "jsonl", "ndjson"],
              "document": ["docx", "pptx", "pdf", "html", "htm", "md", "markdown"]}
    return {"extensions": [e for g in groups.values() for e in g], "groups": groups,
            "max_bytes": 200 * 1024 * 1024,
            "refused": {ext.lstrip("."): why for ext, why in {**UNSUPPORTED, ".xls": "a legacy Excel 97-2003 file; save it as .xlsx"}.items()}}


def sniff(content: bytes, filename: str) -> str:
    """The file's format from its bytes first and its name second (a renamed file is read as what it is)."""
    lower = filename.lower()
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    head = content[:8]
    if head.startswith(b"PAR1"):
        return "parquet"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2: legacy Office, or an encrypted OOXML package
        if ext in (".xlsx", ".xlsm", ".docx", ".pptx"):
            raise ValueError(f"{filename} is password-protected. Remove the password (or save an unprotected copy) and "
                             "upload it again; Lyst never asks for document passwords.")
        raise ValueError(f"{filename} is a legacy Office 97-2003 file. Save it as .xlsx, .docx or .pptx and upload that.")
    if head.startswith(b"PK"):
        try:
            import zipfile  # noqa: PLC0415

            names = set(zipfile.ZipFile(io.BytesIO(content)).namelist())
        except Exception:  # noqa: BLE001
            names = set()
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
    if ext in UNSUPPORTED:
        raise ValueError(f"{filename} is {UNSUPPORTED[ext]}. Nothing was loaded.")
    if ext in FORMATS and FORMATS[ext] not in ("xlsx", "docx", "pptx", "xls"):
        if FORMATS[ext] == "text" and _delimited(content):
            return "csv"
        return FORMATS[ext]
    if ext == ".xls":
        return "xls"
    text = decode(content[:65536]).lstrip()
    if text.startswith(("{", "[")):
        return "json"
    if text[:15].lower().startswith(("<!doctype html", "<html")):
        return "html"
    if _delimited(content):
        return "csv"
    if ext in (".xlsx", ".xlsm", ".docx", ".pptx"):
        raise ValueError(f"{filename} is named like an Office file but is not one (the package is damaged). Nothing was loaded.")
    return "text"


def sniff_delimiter(text: str) -> str | None:
    """The delimiter that splits the most lines into the same number of fields (quotes respected). Python's
    csv.Sniffer picks ',' for "Name;Amount\nA;1,5" because of the decimal commas; consistency across lines does not."""
    lines = [ln for ln in text.splitlines()[:200] if ln.strip()][:60]
    if len(lines) < 2:
        return None
    best, best_score = None, 0.0
    for d in (",", ";", "\t", "|"):
        counts = [len(r) - 1 for r in csv.reader(lines, delimiter=d)]
        fields = [c for c in counts if c > 0]
        if not fields:
            continue
        mode = max(set(fields), key=fields.count)
        score = fields.count(mode) / len(counts) + min(mode, 20) / 1000
        if score > best_score:
            best, best_score = d, score
    return best if best_score >= 0.5 else None


def _delimited(content: bytes) -> bool:
    return sniff_delimiter(decode(content[:32768])) is not None


def read_delimited(content: bytes, filename: str) -> dict[str, Any]:
    text = decode(content)
    sample = text[:65536]
    delim = sniff_delimiter(sample) or ("\t" if filename.lower().endswith(".tsv") else ",")
    encoding = ("utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else
                "utf-8" if _is_utf8(content) else "windows-1252")
    notes = [] if encoding == "utf-8" else [f"The file is {encoding}-encoded; it was converted to UTF-8."]
    stem = filename.rsplit(".", 1)[0]
    if len(content) > GRID_LIMIT:
        import pyarrow.csv as pacsv  # noqa: PLC0415

        table = pacsv.read_csv(io.BytesIO(text.encode("utf-8")), parse_options=pacsv.ParseOptions(delimiter=delim))
        frame = table.to_pandas()
        cols = [{"name": normalise_name(c), "header": c, "type": str(t)} for c, t in zip(table.column_names, table.schema.types, strict=True)]
        t = {"key": "file", "title": None, "sheet": None, "range": None, "notes": [], "proposed_name": proposed_name(stem),
             "rows": table.num_rows, "columns": cols, "header_rows": 1, "totals": [], "preview": preview(frame),
             "warnings": ["Large file: loaded column-wise, without per-cell checks for titles, units or total rows."],
             "include": True}
        return {"kind": "table", "format": "csv", "delimiter": delim, "encoding": encoding, "tables": [(t, frame)],
                "warnings": notes}
    grid = [row for row in csv.reader(io.StringIO(text), delimiter=delim)]
    grid = [[(v if v.strip() else None) for v in row] for row in grid]
    regions = locate_tables(grid) or []
    tables = []
    for n, reg in enumerate(regions[:20], start=1):
        cells = [row[reg.left:reg.right] for row in grid[reg.top:reg.bottom]]
        shaped = shape_table(cells)
        tables.append(({"key": f"rows {reg.top + 1}-{reg.bottom}", "title": reg.title, "sheet": None, "range": None,
                        "notes": reg.notes, "proposed_name": proposed_name(stem, f"table_{n}" if len(regions) > 1 else None),
                        "rows": len(shaped.frame), "columns": shaped.columns, "header_rows": shaped.header_rows,
                        "totals": shaped.totals, "warnings": shaped.warnings, "preview": preview(shaped.frame),
                        "include": n == 1 or len(shaped.frame) > 1}, shaped.frame))
    if regions and regions[0].top > 0:
        notes.append(f"The first {regions[0].top} line(s) are a title or notes, not data, and were left out.")
    return {"kind": "table", "format": "csv", "delimiter": delim, "encoding": encoding, "tables": tables, "warnings": notes}


def _is_utf8(content: bytes) -> bool:
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def read_json(content: bytes, filename: str, *, lines: bool) -> dict[str, Any]:
    text = decode(content)
    try:
        if lines:
            records = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            data = json.loads(text)
            records = data
            if isinstance(data, dict):
                # {"items": [...]} or {"data": [...]}: the largest list of objects is the table
                lists = {k: v for k, v in data.items() if isinstance(v, list) and v and isinstance(v[0], dict)}
                records = max(lists.values(), key=len) if lists else [data]
    except json.JSONDecodeError as exc:
        raise ValueError(f"{filename} is not valid JSON (line {exc.lineno}: {exc.msg}). Nothing was loaded.") from exc
    if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
        raise ValueError(f"{filename} has no list of records to load as a table.")
    frame = pd.json_normalize(records, sep="_")
    for c in frame.columns:
        if frame[c].map(lambda v: isinstance(v, (list, dict))).any():
            frame[c] = frame[c].map(lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v)
    frame.columns = [normalise_name(c) for c in frame.columns]
    cols = [{"name": c, "header": c, "type": str(frame[c].dtype)} for c in frame.columns]
    stem = filename.rsplit(".", 1)[0]
    t = {"key": "records", "title": None, "sheet": None, "range": None, "notes": [], "proposed_name": proposed_name(stem),
         "rows": len(frame), "columns": cols, "header_rows": 0, "totals": [], "preview": preview(frame),
         "warnings": ["Nested lists and objects are kept as JSON text in their column."] if any(
             frame[c].astype(str).str.startswith(("[", "{")).any() for c in frame.columns if frame[c].dtype == object) else [],
         "include": True}
    return {"kind": "table", "format": "ndjson" if lines else "json", "tables": [(t, frame)], "warnings": []}


def read_any(content: bytes, filename: str) -> dict[str, Any]:
    fmt = sniff(content, filename)
    if fmt in ("xlsx", "xls"):
        return read_workbook(content, filename) | {"format": fmt}
    if fmt == "csv":
        return read_delimited(content, filename)
    if fmt in ("json", "ndjson"):
        return read_json(content, filename, lines=fmt == "ndjson")
    if fmt == "parquet":
        table = pq.read_table(io.BytesIO(content))
        frame = table.to_pandas()
        cols = [{"name": normalise_name(c), "header": c, "type": str(t)} for c, t in zip(table.column_names, table.schema.types, strict=True)]
        t = {"key": "file", "title": None, "sheet": None, "range": None, "notes": [], "rows": table.num_rows, "columns": cols,
             "proposed_name": proposed_name(filename.rsplit(".", 1)[0]), "header_rows": 0, "totals": [],
             "preview": preview(frame), "warnings": [], "include": True}
        return {"kind": "table", "format": "parquet", "tables": [(t, table)], "warnings": []}
    return read_document(content, filename, fmt)


def _plan(parsed: dict[str, Any]) -> dict[str, Any]:
    """The plan as stored and returned: everything except the frames and the full text."""
    out = {k: v for k, v in parsed.items() if k not in ("tables", "text")}
    out["tables"] = [t for t, _ in parsed["tables"]]
    if "text" in parsed:
        out["document"] = {"words": parsed.get("words", 0), "pages": parsed.get("pages"),
                           "headings": parsed.get("headings", [])[:50], "excerpt": parsed["text"][:600],
                           "include": bool(parsed["text"].strip())}
    return out


def to_arrow(frame) -> pa.Table:
    if isinstance(frame, pa.Table):
        return frame
    table = pa.Table.from_pandas(frame, preserve_index=False)
    # an all-empty column has Arrow type null, which a database column cannot be: store it as text
    fields = [pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f for f in table.schema]
    return table.cast(pa.schema(fields)) if any(pa.types.is_null(f.type) for f in table.schema) else table


class IngestService:
    def __init__(self, stores: Stores, guard: Guard, data, knowledge) -> None:
        self.stores = stores
        self.guard = guard
        self.data = data
        self.knowledge = knowledge

    def inspect(self, actor: Principal, ws: str, content: bytes, *, filename: str,
                classification: str = "internal") -> dict[str, Any]:
        """Read the file and say what is in it. Nothing becomes a dataset or a document until ``commit``."""
        self.guard.require(actor, ws, "data.ingest", filename)
        if not content:
            raise ValueError(f"{filename} is empty. Nothing was loaded.")
        try:
            parsed = read_any(content, filename)
        except ValueError:
            raise
        except Exception as exc:  # a malformed file is the person's input to fix, never a server error
            log.info("could not read %s: %s", filename, exc)
            raise ValueError(f"{filename} could not be read ({type(exc).__name__}: {str(exc)[:160]}). Nothing was loaded.") from exc
        plan = _plan(parsed)
        jid = "ing_" + uuid.uuid4().hex[:12]
        sha = hashlib.sha256(content).hexdigest()
        plan["duplicate_of"] = self._seen(ws, sha)
        self.stores.workspace(ws).execute(
            "INSERT INTO ingest_jobs (id, filename, kind, format, size_bytes, sha256, status, plan, content, classification, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?)",
            [jid, filename, plan["kind"], plan.get("format", plan["kind"]), len(content), sha, json.dumps(plan, default=str),
             content, classification, actor.user_id])
        self.guard.audit(actor, ws, "ingest.inspect", jid, "ok", {"filename": filename, "tables": len(plan["tables"]),
                                                                  "format": plan.get("format")})
        return {"id": jid, "filename": filename, "status": "staged", "sha256": sha, "size_bytes": len(content), **plan}

    def _seen(self, ws: str, sha: str) -> dict[str, Any] | None:
        rows = self.stores.workspace(ws).query(
            "SELECT id, filename, created_at FROM ingest_jobs WHERE sha256 = ? AND status = 'committed' ORDER BY created_at DESC LIMIT 1", [sha])
        return {**rows[0], "created_at": str(rows[0]["created_at"])} if rows else None

    def get(self, actor: Principal, ws: str, jid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.read", jid)
        rows = self.stores.workspace(ws).query(
            "SELECT id, filename, kind, format, size_bytes, sha256, status, plan, result, classification, created_by, created_at, "
            "finished_at FROM ingest_jobs WHERE id = ?", [jid])
        if not rows:
            raise KeyError(jid)
        r = rows[0]
        plan = json.loads(r.pop("plan"))
        r["result"] = json.loads(r["result"]) if r["result"] else None
        return {**r, **plan}

    def list(self, actor: Principal, ws: str, limit: int = 50) -> list[dict[str, Any]]:
        self.guard.require(actor, ws, "data.read")
        rows = self.stores.workspace(ws).query(
            "SELECT id, filename, kind, format, size_bytes, status, created_by, created_at, finished_at, "
            "json_array_length(json_extract(plan, '$.tables')) AS tables FROM ingest_jobs ORDER BY created_at DESC LIMIT ?",
            [min(limit, 500)])
        return rows

    def discard(self, actor: Principal, ws: str, jid: str) -> dict[str, Any]:
        self.guard.require(actor, ws, "data.ingest", jid)
        job = self.get(actor, ws, jid)
        if job["status"] != "staged":
            raise ValueError(f"this upload is already {job['status']}")
        self.stores.workspace(ws).execute(
            "UPDATE ingest_jobs SET status = 'discarded', content = NULL, finished_at = now() WHERE id = ?", [jid])
        self.guard.audit(actor, ws, "ingest.discard", jid, "ok", {"filename": job["filename"]})
        return {"id": jid, "status": "discarded"}

    def commit(self, actor: Principal, ws: str, jid: str, *, tables: list[dict[str, Any]] | None = None,
               document: bool | None = None, title: str | None = None) -> dict[str, Any]:
        """Load the chosen tables as dataset versions and the document text as cited knowledge.

        ``tables`` items are ``{"key": ..., "name": ..., "include": bool}``; tables not named keep the plan's choice
        and proposed name. ``document`` defaults to the plan's choice."""
        self.guard.require(actor, ws, "data.ingest", jid)
        db = self.stores.workspace(ws)
        found = db.query("SELECT filename, status, plan, content, classification, sha256 FROM ingest_jobs WHERE id = ?", [jid])
        if not found:
            raise KeyError(jid)
        job = found[0]
        if job["status"] != "staged":
            raise ValueError(f"this upload is already {job['status']}; upload the file again to load it again")
        content, filename = bytes(job["content"]), job["filename"]
        parsed = read_any(content, filename)
        plan = json.loads(job["plan"])
        choices = {c["key"]: c for c in (tables or [])}
        unknown = set(choices) - {t["key"] for t, _ in parsed["tables"]}
        if unknown:
            raise ValueError(f"no table {sorted(unknown)[0]!r} in this file")
        loaded, errors = [], []
        for t, frame in parsed["tables"]:
            choice = choices.get(t["key"], {})
            if not choice.get("include", t["include"]):
                continue
            name = normalise_name(choice.get("name") or t["proposed_name"])
            units = [f"{c['name']} in {c.get('unit', '')}{' x' + format(c['scale'], 'g') if c.get('scale') else ''}".strip()
                     for c in t["columns"] if c.get("unit") or c.get("scale")]
            derived = [f"{c['name']} = {c['formula']}" for c in t["columns"] if c.get("formula")]
            desc = " ".join(p for p in [
                f"From {filename}" + (f", {t['key']}" if t["key"] not in ("file", "records") else "") + ".",
                f"Units: {'; '.join(units)}." if units else "", f"Derived: {'; '.join(derived)}." if derived else ""] if p)
            try:
                r = self.data.ingest_arrow(actor, ws, name, to_arrow(frame), source=f"{filename}#{t['key']}",
                                           title=t.get("title") or None, description=desc,
                                           classification=job["classification"])
            except (ValueError, pa.ArrowException) as exc:
                errors.append({"key": t["key"], "name": name, "error": str(exc)[:300]})
                continue
            loaded.append({"key": t["key"], "dataset": r["dataset"], "version": r["version"], "rows": r["rows"],
                           "unchanged": r.get("unchanged", False)})
        doc = None
        want_doc = plan.get("document", {}).get("include", False) if document is None else document
        if want_doc and parsed.get("text", "").strip():
            doc = self.knowledge.add_document(actor, ws, parsed["text"].encode("utf-8"), filename=filename,
                                              title=title or (parsed.get("headings") or [None])[0] or filename,
                                              classification=job["classification"], text=parsed["text"])
            for item in loaded:
                self.knowledge.relate(actor, ws, "document", doc["id"], "contains", "dataset", item["dataset"],
                                      evidence={"table": item["key"], "version": item["version"]})
        if not loaded and doc is None:
            raise ValueError("nothing was chosen to load" if not errors else f"no table could be loaded: {errors[0]['error']}")
        result = {"datasets": loaded, "document": doc, "errors": errors}
        db.execute("UPDATE ingest_jobs SET status = 'committed', content = NULL, result = ?, finished_at = now() WHERE id = ?",
                   [json.dumps(result, default=str), jid])
        self.guard.audit(actor, ws, "ingest.commit", jid, "ok" if not errors else "partial",
                         {"filename": filename, "datasets": [x["dataset"] for x in loaded], "document": doc["id"] if doc else None})
        return {"id": jid, "status": "committed", **result}

    def load(self, actor: Principal, ws: str, content: bytes, *, filename: str, classification: str = "internal") -> dict[str, Any]:
        """Inspect and commit in one step with the plan's own choices (the SDK's and MCP's one-call path)."""
        job = self.inspect(actor, ws, content, filename=filename, classification=classification)
        return {"plan": job, **self.commit(actor, ws, job["id"])}


__all__ = ["IngestService", "formats", "read_any", "sniff"]
