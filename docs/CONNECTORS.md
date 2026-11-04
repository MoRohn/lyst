# Connectors

A connector gives Lyst isolated, least-privilege, read-only access to a source system. Connections belong to
a workspace; secrets live in their own column of the encrypted control database and never leave the service
(the API returns configuration without them). A connection is tested before it is saved.

| Kind | What it reads | Incremental | Notes |
|---|---|---|---|
| `postgres` | Tables and views in the listed schemas | Yes, by a cursor column (`id`, `updated_at`) | psycopg 3; every transaction `READ ONLY`, 60 s statement timeout, server-side cursors, identifiers validated |
| `files` | CSV, TSV and Parquet files in one directory | No (a sync reloads the file) | The directory must be under `LYST_IMPORT_ROOT` (default `$LYST_HOME/imports`) |
| `sqlite` | Tables and views of a SQLite file | Yes, by a cursor column | Opened `mode=ro`; the file must be under the import root |
| `rest` | A JSON list endpoint with page or cursor pagination | Yes, with a `since` parameter and a cursor column | GET only, https only, hosts limited by `LYST_REST_ALLOWED_HOSTS`; bearer or API-key header; nested values stored as JSON text |
| `http_file` | One CSV, TSV, Parquet or JSON file at a URL (a published extract, a portal download, an object-store link) | Yes, by cursor column: the file is downloaded again and only newer rows are appended | GET only, https only, same host allow-list; bearer or API-key header; at most `LYST_HTTP_FILE_MAX_MB` (200); JSON lines and nested keys supported |

## Syncing

`sync` loads a source table into a governed dataset. The first sync is a full load; with a cursor column,
later syncs read only rows past the last cursor value and the new dataset version is the previous version's
rows plus the new ones. Every sync is a dataset version with a hash, so earlier states stay reproducible, and
an audit line. A sync can be scheduled (`sync connection <id> table <t> by <cursor>`), which goes through the
same approval as any schedule and runs as its owner.

## Operations: errors, retries, health

Every test, discovery and read goes through one path (`ConnectionService._attempt`):

* **One error vocabulary.** Failures are classified as `auth`, `permission`, `rate_limit`, `timeout`, `network`,
  `source`, `not_found`, `schema`, `config` or `unknown` (`connectors/reliability.py`), from a category the
  connector set (`ConnectorError(..., category="rate_limit", retry_after=…)`, as the REST and URL-file connectors do
  for HTTP status codes) or from the message. Each category has one sentence saying what the person should do.
* **One retry policy.** Network, timeout, rate-limit and source errors are retried with exponential backoff and
  jitter, honouring `Retry-After`, up to the manifest's `retry_attempts` (3). Auth, permission, not-found, schema and
  config errors fail at once: retrying them hides the cause.
* **Events.** Each operation is a row in `connection_events` (action, table, outcome, category, scrubbed message,
  attempts, the categories retried, duration). Schema changes on a sync are events too.
* **Health.** `GET /v1/workspaces/{ws}/connections/health` gives every connection a state (`healthy`, `degraded`,
  `failing`, `needs_action`, `untested`), the reasons, the required action, last success and failure, consecutive
  failures, the credential's id and state (never its value), each synced table's last sync, schedule, overdue flag,
  data freshness and contract outcome, and the last day's operations, errors, retries and rate limits. Three
  transient failures in a row, or any permanent one, put a finding in the inbox of the connection's creator and the
  workspace's owners and admins, once a day per cause.
* **Schema evolution.** A sync compares what the source returned with the dataset's current version. Added and
  removed columns are recorded; a column that changed type on an incremental sync triggers a full reload, so a
  version never mixes types. The dataset's data contract, if it has one, then reports the change as breaking with
  everything downstream that is affected.

## Writing a connector

Add a module in `src/lyst/connectors/` with a class that declares a `Manifest` (kind, label, fields with
their `secret` flag, capabilities) and implements `test()`, `discover() -> list[SourceTable]` and
`read(table, cursor_column, after) -> Iterator[pyarrow.Table]`, decorated with `@register`. Nothing in the
core application changes; the API and the Data › Connections page read the manifest.

Declare `auth` (`none`, `password`, `token`, `api_key`) and, if the source needs it, `retry_attempts`. Raise
`ConnectorError` with a `category` when you know it (HTTP status codes map directly); the service classifies the rest.

Rules every connector keeps: it reads, never writes; it validates identifiers it puts into SQL; it fails
loudly on a connection problem so that nothing untestable is saved; it never logs secrets.

## Not yet built

MySQL, SQL Server, Snowflake, BigQuery, Redshift, Databricks, GraphQL, SaaS-specific adapters (Salesforce,
Workday, NetSuite, ServiceNow, Jira; many of their list APIs already work through `rest`), object storage.
They are not written because no server is available here to test them against; an untested connector is not shipped. The SDK is the path for each; the commercial ones stay
optional integrations behind the same interface.
