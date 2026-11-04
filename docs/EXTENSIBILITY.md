# Extensibility, portability and reproducibility

The register for the lane that makes Lyst take in any file, be driven from code, reproduce its own work, move
between installations and run without a network. Each section says what exists, how it is tested, and what is
still missing. Migrations for this lane use versions 40-49 (control and workspace).

## 1. Multimodal ingestion (`src/lyst/ingest/`)

One door for files: `POST /v1/workspaces/{ws}/ingest` (multipart `file`, optional `classification`, `commit`).

**Look first, then load.** `inspect` reads the file and returns a plan; nothing becomes a dataset until `commit`
(`POST .../ingest/{id}/commit` with `{"tables": [{"key", "name", "include"}], "document": bool}`). `commit=true` on
upload loads the plan's own choices in one call (for scripts and the SDK). `DELETE .../ingest/{id}` discards a
staged file. `GET /v1/ingest/formats` lists the extensions read (grouped spreadsheet, table, document), the size
limit and the refused types with the reason, so an uploader's file picker stays in sync. CSV, TSV and Parquet return one
table (a CSV with several blocks separated by blank lines returns each block; the first is included). The staged bytes live in the encrypted workspace database (`ingest_jobs`, workspace migration 40) and
are dropped on commit or discard; the SHA-256 stays, so a re-upload of the same file says which earlier upload it
duplicates.

| Format | What is read |
|---|---|
| Excel .xlsx/.xlsm | Every table on every sheet, split by blank rows and columns; a title line above and footnotes below kept as context; merged and two-row headers folded ("FY2026 Revenue"); units and scale from headers ("Revenue (USD m)" is USD x 1e6); formula columns reported as derived with the formula's shape (`=B{row}-C{row}`); formulas with no saved result flagged; spreadsheet errors (#DIV/0!) loaded as empty and counted; total rows removed from the data and checked against the sum of the rows; hidden sheets offered but not loaded by default, hidden rows left out |
| Word .docx | Text with headings for cited search; each table offered as a dataset, named after the heading above it |
| PowerPoint .pptx | Slide titles, text and speaker notes; slide tables as datasets |
| HTML, Markdown | Text (scripts and styles dropped); `<table>` (with captions, colspan) and pipe tables as datasets |
| PDF | Text layer for cited search; a page count and a warning when the PDF looks scanned |
| JSON, NDJSON | The largest list of records, nested objects flattened (`customer_name`), lists kept as JSON text |
| CSV/TSV/TXT | Delimiter by consistency across lines (handles `;` with decimal commas), encoding (UTF-8, UTF-16, Windows-1252), title lines skipped, same column intelligence as workbooks; files above 20 MB load column-wise |
| Parquet | As before |

Refused, with the fix in the message: legacy .xls/.doc/.ppt, password-protected Office files (Lyst never asks for
document passwords), images (no bundled OCR), archives, damaged packages, XML that declares entities.

Each loaded table is a new dataset version through `DataService.ingest_arrow`, so contracts, catalog insights and
asset health run as for any upload. Its source reads `file#Sheet!A3:D8`, and its description carries units and
derived-column formulas. When the file is a document, the text goes into knowledge search and lineage records
`document contains dataset`.

Tests: `tests/test_ingest.py`.

Gaps: OCR for scans and images (would need Tesseract, not bundled); tables inside PDFs; .ods; workbook-level
named ranges and Excel Table objects (ListObjects) are found by layout, not by name; pivot caches are not read.
