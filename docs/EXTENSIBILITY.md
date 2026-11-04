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

**Nothing in a file is ever run.** Workbooks are read for their saved cell values; formulas are never evaluated
(a formula without a saved result loads empty and is flagged). Macros (VBA), embedded OLE objects, ActiveX controls
and links to other workbooks are detected, reported in the plan (`active_content` and a warning) and ignored:
never opened, extracted, followed or run. PDFs are read for their text layer only (their scripts never run); web
pages lose `<script>`, `<style>` and templates before reading; XML that declares entities is refused. Tested by
`test_macros_embedded_objects_and_formulas_are_never_run` (a macro-enabled workbook with an embedded object, an
ActiveX control, an external link and a `WEBSERVICE` formula pointed at an outside address).

Refused, with the fix in the message: legacy .xls/.doc/.ppt, password-protected Office files (Lyst never asks for
document passwords), images (no bundled OCR), archives, damaged packages, XML that declares entities.

Each loaded table is a new dataset version through `DataService.ingest_arrow`, so contracts, catalog insights and
asset health run as for any upload. Its source reads `file#Sheet!A3:D8`, and its description carries units and
derived-column formulas. When the file is a document, the text goes into knowledge search and lineage records
`document contains dataset`.

Tests: `tests/test_ingest.py`.

Gaps: OCR for scans and images (would need Tesseract, not bundled); tables inside PDFs; .ods; workbook-level
named ranges and Excel Table objects (ListObjects) are found by layout, not by name; pivot caches are not read.

## 2. One event model (`src/lyst/events.py`)

Everything that happens becomes an event in `platform_events` (control migration 40). Events are derived, not
emitted: a collector on the worker tick reads the audit log and the tasks table from stored cursors, so no service
is instrumented twice and a new audited action becomes an event with one line in `CATALOG`.

```json
{"seq": 812, "id": "a:5531", "type": "dataset.version_created", "schema": 1, "workspace_id": "northwind",
 "occurred_at": "2026-11-04T19:02:11+00:00", "actor": "u_9f..", "subject": {"kind": "dataset", "id": "sales"},
 "data": {"version": 3, "rows": 1200}, "source": "audit"}
```

`data` holds ids, versions, counts and outcomes from the already-redacted audit detail; never answer text or row
values. Readers: `GET /v1/events/types`; `GET /v1/workspaces/{ws}/events?after=&types=dataset.*,task.*&wait=25`
(long-poll; `after=-1` means "from now"; returns `{events, next}`); in process, `lyst.events.subscribers.append(fn)`
and `lyst.events.since(ws, after, types=)`. Members see shared changes (datasets, metrics, contracts, assets...) and
their own tasks; membership, approvals and others' tasks need `audit.read`. Retention follows
`LYST_RETENTION_DAYS_EVENTS` (90 days).

## 3. Signed webhooks (`src/lyst/webhooks.py`)

`POST /v1/workspaces/{ws}/webhooks {url, types, description}` (admins) returns the signing secret once.
Deliveries carry `Lyst-Event`, `Lyst-Delivery` (stable across retries) and `Lyst-Signature: t=..,v1=HMAC-SHA256("t.body")`.
Retries at 30 s, 1, 2, 4 ... minutes (capped at 6 h), eight attempts, then dead; 400/401/403/404/410 are dead after
three. Twenty dead deliveries in a row pause the subscription with the reason. `PATCH` re-activates or rotates the
secret; `/ping` sends a signed ping now; `/deliveries` lists attempts; `/webhook-deliveries/{id}/redeliver` replays.
Targets must be https and must not resolve to private, loopback or link-local addresses (checked at creation and
at every delivery; `LYST_WEBHOOK_ALLOW_PRIVATE=1` for closed networks). The older single `webhook_url` setting for
monitor findings is unchanged.

Gap: the signing secret is stored in the encrypted control database rather than the credential store.

## 4. API contract and Python SDK

`docs/api/contract.txt` records every operation and what a caller must send. `tests/test_api_contract.py` fails on
a removed operation or a new required query parameter or body field; additions are compatible and recorded with
`uv run python scripts/api_contract.py --write`. Every response carries `Lyst-API-Version: 1`.

`sdk/python` is the `lyst-client` package (httpx only, pandas optional): `Lyst(url, token).workspace(ws)` with
`files.inspect/commit/load/formats`, `ask` (idempotency key, waits, raises `TaskFailed`), `submit`, `sql` (to
records or pandas), `events.list/stream`, `webhooks.*`, `search`, `datasets`, `metrics`; typed errors
(`AuthError`, `NotFound`, `Invalid`, `RateLimited` with `retry_after`, `ServerError`); retries on 429/502/503/504
for reads and idempotent writes, honouring `Retry-After`; a warning when the engine's API version is newer;
`verify_signature` for webhook receivers. Tests: `tests/test_sdk.py` runs the SDK against the real app.
Not yet published to PyPI; install with `pip install ./sdk/python`.

## 5. MCP hardening (`src/lyst/mcp_server.py`)

Every tool declares MCP annotations (read-only and idempotent for lookups and SQL; `ask` and `ingest_file` are
work, never destructive). Every call is audited as `mcp.<tool>` with its outcome. Results above
`LYST_MCP_MAX_BYTES` (256 KB) are cut to fit, rows first, with `truncated`, the total and a note. New tools:
`events` (what changed since a cursor) and `ingest_file` (base64, 20 MB at most, inspect or load).

## 6. Experiment registry (`src/lyst/experiments.py`, workspace migration 41)

An experiment states a question, a kind and a spec; running it records one run per arm with its inputs (metric
version, dataset versions, the executed SQL and the exact series), the environment (`src/lyst/environment.py`:
Python and the versions of the packages that decide numbers, plus the lockfile digest), scores and digests.

* **forecast**: rolling-origin backtests of `naive`, `seasonal_naive`, `mean`, `drift`, `linear_trend` and `ets` (the
  analyst's default) on an approved metric at a grain. Scores: MASE (primary by default), MAE, RMSE, MAPE, sMAPE,
  bias, interval coverage. A winner is named only when it beats the runner-up on paired absolute errors (Wilcoxon
  signed-rank, one-sided, 5%); otherwise the verdict is "no clear winner" and the simplest tied method is
  recommended. The comparison also says where the analyst's default ranks and which methods' intervals are
  calibrated (coverage within 15 points of the nominal 80%).
* **eval**: the evaluation bank as an experiment arm; later runs are compared with the first using
  `lyst.evals.compare` when the regression gate is installed (ops thread), else a per-case pass comparison.

`POST .../experiment-runs/{id}/reproduce` recomputes a forecast run on its recorded series and returns
`{same, differences, observed_changed, environment}` (the scenario reproduction shape): `observed_changed` says
today's data differs from what the run saw; `environment` lists package or Python changes. `POST .../decision`
records the chosen arm with a note and whether it goes against the evidence; a decided experiment is closed.

Routes: `GET /v1/experiments/methods`, `GET|POST /v1/workspaces/{ws}/experiments`, `GET .../experiments/{id}`,
`POST .../{id}/run`, `GET .../{id}/comparison`, `POST .../{id}/decision`, `GET|POST .../experiment-runs/{id}[/reproduce]`.
Permissions: `experiment.run` (editor), `experiment.decide` (admin). Events: `experiment.created|ran|decided`.

Gaps: the decision is advice; the analyst's forecast step does not yet read it. No causal-method arms yet
(interrupted time series vs difference-in-differences), and no hyperparameter search.

## 7. Execution environment and workbench (`src/lyst/environment.py`, `sandbox.py`, `workbench.py`, workspace migration 42)

`GET /v1/environment` says what computations run on (Python, the numeric packages that decide numbers, the
lockfile digest) and what the workbench offers on this host.

**SQL**: `POST /v1/workspaces/{ws}/workbench/sql` is the guarded read-only query (history kept as for every query).

**Python**: `POST /v1/workspaces/{ws}/workbench/python {code, inputs: {name: sql}, timeout}` (editors). The inputs run
first as guarded SQL under the caller's column and row policies; the code then runs in a separate interpreter
inside an OS sandbox and sees them as pandas DataFrames; whatever it assigns to `result` comes back (a table as
columns, row count and a 50-row preview; a value as JSON), with stdout and a readable error.

| Host | Sandbox | Verified here |
|---|---|---|
| macOS | `sandbox-exec`, deny by default: reads only from the system, the Python installation, the packages and the job folder; writes only to the job folder; no network | Yes: `test_the_sandbox_contains_the_code` reads the Lyst home, `~/.ssh` and a temp file, opens a socket and writes to /tmp, all refused; no `LYST_*` variables |
| Linux | `bwrap`: every namespace unshared (no network), read-only root, Lyst home and credential folders hidden, job folder writable | No (no Linux host); written to bubblewrap's documented flags |
| Other / `LYST_SANDBOX=off` | Python workbench off, with the reason | Yes |

Limits: 60 s wall clock (up to 300 on request), 60 CPU-seconds, 256 MB file writes, 2 GB address space on Linux,
200,000 rows per input, single-threaded numeric libraries, a scrubbed environment. The sandbox contains an editor's
own analysis code; it is not offered as a boundary against hostile code from strangers.

Every run is recorded (code, inputs with SQL, dataset versions, row counts and row digests, environment, outcome,
result digest). `.../runs/{id}/reproduce` reruns it and answers `{same, differences, observed_changed,
changed_inputs, environment}`. `.../runs/{id}/save {name}` stores a table result as a dataset version with source
`workbench:<run>` and `derived_from` lineage to what it read. Members see their own runs; admins see all.

Gaps: task-level reproduction will build on the adapt thread's `evidence.reproduce` (re-run recorded queries)
once it merges, adding result digests (its gap E2) and the environment diff; Docker/Kubernetes images for the
sandbox are not provided (no Docker on this host).
