# Remaining gaps and edge cases

A sweep of everything still open after the three master prompts, the secrets specification and the dashboard-kit
rebuild (2026-11-04, main at `99d7d1d`). Every line says what is true now, not what is planned. Tests for the fixes are in `tests/test_gaps.py`. Product-experience
items live in `docs/PRODUCT_EXCELLENCE.md` (the product-excellence thread) and are not repeated here.

Status words: **open** (nothing done), **in progress**, **done** (with the commit), **waiting** (needs something
only Rohn or an environment can provide), **by design** (not a defect; documented).

## Security and secrets

| # | Gap or edge case | Status | Notes |
|---|---|---|---|
| S1 | Row-level security: column policies are the finest grain, so a viewer who may read a dataset reads every row | done (this sweep) | Row policies per dataset column, by user or role, applied inside the SQL guard so every path (Analyst, SQL, MCP, dashboards, monitors) obeys them |
| S2 | A connection's secret cannot be rotated in place: Replace on a connection credential would overwrite the JSON payload with a bare string | done (this sweep) | Replace a connection credential by its secret field, re-test through the connector, keep the payload shape |
| S3 | A connection whose credential was revoked raises a 503 on test or sync instead of marking the connection in error | done (this sweep) | Mark the connection `error` with a plain reason |
| S4 | Two concurrent "Initialise encryption" clicks race; the second answers with a 500 | done (this sweep) | A lock around initialisation; a clear 409 |
| S5 | CLI commands in setup mode (backup, prune, token) end in a traceback instead of a sentence | done (this sweep) | One message naming the fix |
| S6 | The audit chain is verified only when an administrator opens the Installation tab | done (this sweep) | Verified at startup (logged, exported as a metric) |
| S7 | Personal OpenRouter keys (policy "people may connect a personal key") are stored but never used by the engine, because the gateway only knows the workspace | done (this sweep) | The gateway resolves the task's owner, so a personal key applies to that person's own tasks |
| S8 | OpenRouter keys cannot be revoked at the provider from Lyst; revoking in Lyst stops Lyst and says so | by design, optional | OpenRouter's provisioning API can delete keys if an administrator connects a provisioning key; not built (needs a decision: do you want Lyst to hold a key that can delete other keys?) |
| S9 | `docs/SECURITY.md` still says master-key rotation is manual | done in this sweep | `lyst rotate-master-key` exists since `11d5f85` |
| S10 | OIDC exercised against a stub provider only | waiting | A real IdP login (Keycloak, Authentik, Google) needs an issuer and client from Rohn |
| S11 | The skill sandbox is process isolation with an allow-list, not a kernel sandbox | by design | Run in the provided container; `skill.propose` for trusted roles |
| S12 | Plain HTTP; TLS at the proxy | by design | Documented in DEPLOYMENT |
| S13 | Membership changes do not clear the broker's hourly access-decision cache (audit dedup only; access is re-checked on every call) | by design | Documented here; no exposure |

## Models

| # | Gap | Status | Notes |
|---|---|---|---|
| M1 | No real OpenRouter call has run end to end | waiting | Rohn connects the key in Settings; then models, embeddings and dashboard wording get one live run each, reported with the gateway's own call records |
| M2 | No local model run end to end | waiting | No local model server on this machine; the path is covered by the mocked tests |

## Data and connectors

| # | Gap | Status | Notes |
|---|---|---|---|
| D1 | Connectors: PostgreSQL, files, SQLite, REST only | open | MySQL/SQL Server/BigQuery/Snowflake need servers to test against; the SDK is in place. An HTTP file connector (CSV/Parquet/JSON by URL with ETag-based incremental pulls) is testable here and is next if time allows |
| D2 | A dashboard cannot be retired or deleted | done (this sweep) | `retire` keeps the folder for the bundle but stops refreshes and hides it |
| D3 | Dashboards built by a row-restricted user contain only that user's rows | by design | The manifest records who built it; the Build page says so |
| D4 | Backup contains the sealed credential blobs; a restore into an installation with a different master key leaves them unreadable (reported as "secret unavailable", never garbage) | by design | `lyst restore` now checks the key against every file first; the same key opens the blobs |

## Operations

| # | Gap | Status | Notes |
|---|---|---|---|
| O1 | Docker Compose never run | waiting | No Docker on this machine; needs a host with Docker |
| O2 | One engine process per set of database files | by design | `docs/DUCKDB.md`; revisit for multi-node |
| O3 | `opentelemetry.context: Failed to detach context` in the test log | owned by the product-excellence register (their O4) | |
