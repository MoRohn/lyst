# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Document intelligence: the text of a document for cited search, its outline, and the tables inside it.

Word (.docx) is read straight from its XML with the standard library, PowerPoint with python-pptx, PDF text
with pypdf, HTML with the standard parser, Markdown and plain text as they are. Tables found inside a document
(Word tables, slide tables, HTML tables, Markdown pipe tables) are offered as datasets beside the document.
Scanned PDFs and images carry no text layer; Lyst has no bundled OCR, so it says that rather than loading
nothing quietly.
"""

from __future__ import annotations

import io
import re
import zipfile
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET  # noqa: S405 - the XML is the uploaded document's own, parsed without DTDs

from .tables import blank, preview, proposed_name, shape_table

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _safe_xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise ValueError("the document's XML declares entities, which Lyst does not expand")
    return ET.fromstring(data)  # noqa: S314 - DTD/entity declarations are refused above


def read_docx(content: bytes) -> tuple[str, list[str], list[tuple[str, list[list[Any]]]]]:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        root = _safe_xml(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    lines, headings, tables = [], [], []
    for el in list(body) if body is not None else []:
        if el.tag == f"{W}p":
            text = "".join(t.text or "" for t in el.iter(f"{W}t")).strip()
            style = el.find(f"{W}pPr/{W}pStyle")
            sval = style.get(f"{W}val", "") if style is not None else ""
            if text:
                if sval.lower().startswith(("heading", "title")):
                    headings.append(text)
                    lines.append("")  # a heading starts a new passage
                lines.append(text)
        elif el.tag == f"{W}tbl":
            grid = []
            for tr in el.iter(f"{W}tr"):
                grid.append(["".join(t.text or "" for t in tc.iter(f"{W}t")).strip() or None for tc in tr.iter(f"{W}tc")])
            tables.append((headings[-1] if headings else f"Table {len(tables) + 1}", grid))
            lines.append("")
    return "\n\n".join(_paragraphs(lines)), headings, tables


def _paragraphs(lines: list[str]) -> list[str]:
    out, cur = [], []
    for ln in lines:
        if not ln:
            if cur:
                out.append("\n".join(cur))
            cur = []
        else:
            cur.append(ln)
            if len(ln) < 80 and not ln.endswith((".", "!", "?", ":")):  # a short heading line stands alone
                out.append("\n".join(cur))
                cur = []
    if cur:
        out.append("\n".join(cur))
    return out


def read_pptx(content: bytes) -> tuple[str, list[str], list[tuple[str, list[list[Any]]]]]:
    from pptx import Presentation  # noqa: PLC0415

    prs = Presentation(io.BytesIO(content))
    parts, headings, tables = [], [], []
    for i, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.text else f"Slide {i}"
        headings.append(title)
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                grid = [[(c.text.strip() or None) for c in row.cells] for row in shape.table.rows]
                tables.append((title, grid))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"Speaker notes: {notes}")
        parts.append(title)
        if texts:
            parts.append(" ".join(" ".join(t.split()) for t in texts) + ".")
    return "\n\n".join(parts), headings, tables


class _HTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.headings: list[str] = []
        self.tables: list[tuple[str, list[list[Any]]]] = []
        self._buf: list[str] = []
        self._skip = 0
        self._table: list[list[Any]] | None = None
        self._row: list[Any] | None = None
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None
        self._heading = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            span = dict(attrs).get("colspan")
            self._span = int(span) if span and span.isdigit() else 1
        elif tag == "caption" and self._table is not None:
            self._caption = []
        elif tag in ("p", "div", "li", "br", "section", "article"):
            self._flush()
        elif re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self._heading = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            v = " ".join("".join(self._cell).split()) or None
            self._row.extend([v] * getattr(self, "_span", 1))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "caption" and self._caption is not None:
            self._caption_text = " ".join("".join(self._caption).split())
            self._caption = None
        elif tag == "table" and self._table is not None:
            title = getattr(self, "_caption_text", None) or (self.headings[-1] if self.headings else f"Table {len(self.tables) + 1}")
            self.tables.append((title, self._table))
            self._table, self._caption_text = None, None
        elif re.fullmatch(r"h[1-6]", tag) and self._heading:
            text = " ".join("".join(self._buf).split())
            if text:
                self.headings.append(text)
            self._flush()
            self._heading = False
        elif tag in ("p", "div", "li", "section", "article"):
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)
        elif self._table is None:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if text:
            self.blocks.append(text)
        self._buf = []


def read_html(content: bytes) -> tuple[str, list[str], list[tuple[str, list[list[Any]]]]]:
    p = _HTML()
    p.feed(decode(content))
    p.close()
    p._flush()  # noqa: SLF001
    return "\n\n".join(p.blocks), p.headings, p.tables


_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def read_markdown(content: bytes) -> tuple[str, list[str], list[tuple[str, list[list[Any]]]]]:
    text = decode(content)
    lines = text.splitlines()
    headings, tables, keep = [], [], []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("#"):
            headings.append(ln.lstrip("# ").strip())
        if _PIPE_ROW.match(ln) and i + 1 < len(lines) and _PIPE_RULE.match(lines[i + 1]):
            grid = [_pipe_cells(ln)]
            i += 2
            while i < len(lines) and _PIPE_ROW.match(lines[i]):
                grid.append(_pipe_cells(lines[i]))
                i += 1
            tables.append((headings[-1] if headings else f"Table {len(tables) + 1}", grid))
            keep.append("")
            continue
        keep.append(ln)
        i += 1
    return "\n".join(keep), headings, tables


def _pipe_cells(line: str) -> list[Any]:
    return [c.strip() or None for c in line.strip().strip("|").split("|")]


def read_pdf(content: bytes) -> tuple[str, list[str], list[tuple[str, list[list[Any]]]], int]:
    from pypdf import PdfReader  # noqa: PLC0415

    reader = PdfReader(io.BytesIO(content))
    pages = [(page.extract_text() or "") for page in reader.pages]
    outline: list[str] = []
    try:
        for item in reader.outline:
            if not isinstance(item, list) and getattr(item, "title", None):
                outline.append(item.title)
    except Exception:  # noqa: BLE001,S110 - an unreadable outline is not a reason to refuse the text
        pass
    return "\n\n".join(pages), outline, [], len(pages)


def decode(content: bytes) -> str:
    """UTF-8 (with or without BOM), then UTF-16 with a BOM, then Windows-1252, which never fails."""
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def read_document(content: bytes, filename: str, kind: str) -> dict[str, Any]:
    warnings: list[str] = []
    pages = None
    if kind == "docx":
        text, headings, raw_tables = read_docx(content)
    elif kind == "pptx":
        text, headings, raw_tables = read_pptx(content)
    elif kind == "html":
        text, headings, raw_tables = read_html(content)
    elif kind == "markdown":
        text, headings, raw_tables = read_markdown(content)
    elif kind == "pdf":
        text, headings, raw_tables, pages = read_pdf(content)
        if pages and len(text.strip()) < 40 * pages:
            warnings.append("This PDF has little or no text layer (it looks scanned). Lyst has no built-in OCR, so only "
                            "the text it found is searchable; export a text PDF, or run OCR first, for full coverage.")
        warnings.append("Tables inside PDFs are not extracted as datasets; their text is searchable.") if text.strip() else None
    else:
        text, headings, raw_tables = decode(content), [], []
    stem = filename.rsplit(".", 1)[0]
    tables = []
    for n, (title, grid) in enumerate(raw_tables, start=1):
        grid = [r for r in grid if any(not blank(v) for v in r)]
        if len(grid) < 2:
            continue
        shaped = shape_table(grid)
        key = f"table {n}"
        tables.append(({"key": key, "title": title, "range": None, "sheet": None, "notes": [],
                        "proposed_name": proposed_name(stem, title if title and not title.startswith("Table ") else f"table_{n}"),
                        "rows": len(shaped.frame), "columns": shaped.columns, "header_rows": shaped.header_rows,
                        "totals": shaped.totals, "warnings": shaped.warnings, "preview": preview(shaped.frame),
                        "include": len(shaped.frame) >= 2 and len(shaped.columns) >= 2}, shaped.frame))
    names = [t["proposed_name"] for t, _ in tables]
    for i, (t, _) in enumerate(tables, start=1):
        if names.count(t["proposed_name"]) > 1:
            t["proposed_name"] = proposed_name(stem, f"table_{i}")
    words = len(text.split())
    if not text.strip():
        warnings.append("No readable text was found in the document.")
    return {"kind": "document", "format": kind, "text": text, "headings": headings[:200], "words": words, "pages": pages,
            "tables": tables, "warnings": [w for w in warnings if w]}
