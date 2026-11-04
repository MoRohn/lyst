# Platform capability evolution (iteration 7)

This is the working register for the seventh master prompt ("enterprise intelligence and platform capability
evolution"). It holds the repository assessment the prompt asks for, what each pull request delivered, and the
prioritized record of what is still missing. Every statement says what is true in code on `main` plus the PRs named,
not what is planned. Product-experience items stay in `docs/PRODUCT_EXCELLENCE.md` and `docs/UX_STRATEGY.md`;
engineering hardening stays in `docs/ENGINEERING_EXCELLENCE.md`; older gaps stay in `docs/GAPS.md`.

Rohn's order for this iteration: **(1) data intelligence and semantic modeling, (2) connector maturity,
(3) artifact intelligence and lifecycle, (4) team collaboration**. Each is delivered as a complete vertical slice
(store, service, API, MCP where it helps, tests, docs); the interface for each is handed to the UX thread, which owns
`src/lyst/ui/` (see "Where it surfaces").

## Repository assessment (main at `425ee8c`, 277 tests passing)

| Workstream in the prompt | What existed | Gap that mattered most |
|---|---|---|
| 1.1 Discovery | Versioned datasets with schema and a profile (roles, nulls, PII patterns, hierarchies, suggestions); connector `discover` lists source tables | No keys or grain, no relationships between datasets, no notion of which source is authoritative, no search |
| 1.2 Profiling | `analytics/profile.py` per dataset version | No outliers by robust deviation, no missingness concentration or co-missingness, no time-series continuity, no freshness, no referential checks |
| 1.3 Transformation | Read-only SQL guard; dashboards materialize verified extracts | No derived datasets or transformation pipeline at all (largest open item in data engineering) |
| 1.4 Data contracts | Workflows go stale when a schema fingerprint changes | No declared expectations, no checks on ingest, no impact list, no notification |
| 2.x Connectors | SDK with manifests; PostgreSQL, SQLite, REST JSON, files, files at a URL; sealed secrets with rotation; incremental cursor syncs; scheduled syncs; scrubbed errors | No uniform health record (last success, last failure class, required action), no retry policy or rate-limit handling shared by connectors, no schema-evolution handling on sync, no generated-connector path |
| 3.x Knowledge | Entities and relations with automatic lineage (metric → dataset → column, analysis → metric/dataset, artifact → analysis, dashboard → metric); documents with BM25 + embeddings; consistency with prior answers | Glossary had no quality checks, metrics had no lifecycle (deprecate, replace) or readable history, no "where does this come from" answer |
| 3.2 Semantic layer | Versioned metrics (simple, ratio, filtered), synonyms, glossary, ambiguity surfaced | No owner, grain or currency on a definition; no fiscal calendar; no source precedence |
| 4.x Automation | Workflow compiler, approval-gated schedules, monitors with findings and inbox/webhook | No multi-step workflow composition with branches or approval gates; no natural-language schedule compiler |
| 5.x Artifacts | Excel, Markdown, PDF, slides from an analysis; dashboards with verification, refresh, retire | No unified registry across kinds (artifacts and dashboards are separate tables), no versions or validation status per artifact, no staleness detection when sources change |
| 6.x Collaboration | Roles (viewer…owner), membership, approvals by another role, audit | No sharing of an investigation, comments, review requests, ownership transfer, or notification preferences |
| 7 Search | Document search only | No search across datasets, metrics, workflows, dashboards |
| 8 Personalization | Depth preference in the UI | Most of the list |
| 9 Administration | Settings sections, members, credentials, installation, backup | Usage and budgets per workspace exist for models only |
| 10 Health | `/health`, `/metrics`, OTel, audit-chain check | No single diagnostics view of workers, queues, connections and backups |
| 11 Extensibility | Connector SDK, governed skills with sandbox tests, MCP server | No plugin manifest for extensions beyond skills and connectors |
| 12 Cross-system | Two-source reconciliation only inside the data-quality investigation | No reusable reconciliation or entity matching |

## PR A: data intelligence and semantic modeling (#25, merged)

Built in `src/lyst/catalog.py`, `src/lyst/contracts.py` and `src/lyst/semantic/`; workspace migration 9
(`dataset_insights`, `data_contracts`, `contract_checks`, `metrics.replaced_by`); tests in `tests/test_catalog.py`.

* **Insights on every dataset version**, computed in DuckDB when the version lands (an ingest hook that never fails
  the ingest): grain (smallest unique set of non-measure columns, checked on a sample then on the full table), key
  candidates, duplicates at the grain with an example, a business meaning per column (date, amount, percent, count,
  quantity, category, geography, identifier, free text, personal data), robust outliers (more than 5 MADs), where
  missing values concentrate and which columns go missing together, time-series continuity at the grain the dates
  are recorded at (gaps, and series that lack periods others have), freshness of the latest period, and a ranked
  list of quality risks with a one-sentence summary.
* **Relationships across datasets**, recomputed after every ingest and on demand: foreign-key candidates (values
  contained in another dataset's key), shared dimensions, inconsistent definitions (type mismatch, disjoint values,
  values that match only when case and spacing are ignored), and **overlapping sources** (same measures at the same
  grain) reconciled by totals. Written to the knowledge graph with evidence, so lineage walks include them.
* **Data contracts**: drafted from the current version (types, required columns, key, accepted values for small
  categorical columns, non-negative amounts, references found by discovery, owner), saved as versions by admins
  (`contract.define`), checked on every new version and by hand. Results are breaking (column removed or retyped),
  fail (required nulls, duplicate key, unexpected values, out of range, orphan references) or warn (stale, new
  columns). A breaking or failing check lists the downstream metrics, workflows, dashboards, monitors, analyses and
  artifacts, and raises one deduplicated finding in the inbox of the contract owner and the workspace's owners and
  admins. Data is never rejected; every version stays.
* **Semantic layer**: metric definitions carry an owner, a grain and an ISO currency; metrics can be **deprecated**
  with a replacement (requests and glossary terms that name the old metric resolve to the new one; a new definition
  brings a retired metric back); **history** with field-level changes and the compiled SQL of every version
  ("what changed in our procurement definition?"); a **semantic audit** for duplicate definitions, the same
  calculation on two sources, synonyms that make requests ambiguous, glossary terms pointing at missing or retired
  metrics, undefined terms and terms without provenance, definitions reading columns their dataset lost, and
  definitions missing an owner, description or currency.
* **Metric lineage** (`explain_metric`): definition and SQL, dataset version and origin (file or connection and
  table), grain and freshness, contract status, quality notes on the columns it reads, related and overlapping
  sources, downstream dashboards, workflows, monitors, analyses and artifacts, and the version history.
* **Search** across metrics, datasets, columns, glossary terms, saved workflows, dashboards, earlier analyses and
  documents, with glossary expansion, approved definitions ranked first among equals, and each hit's connected
  assets. Column policies hide restricted columns; row-limited readers get no value-level detail.
* **Interfaces**: REST under `/v1/workspaces/{ws}/catalog…`, `/datasets/{name}/insights|impact|contract…`,
  `/metrics/{name}/lineage|history|deprecate`, `/semantic/audit`; MCP tools `search` and `metric_lineage`.

Verified: 288 tests pass (11 new), `ruff`, `lyst eval` 19/19 and the license check pass. On the demo workspace the
catalog finds the planted ledger double booking (one repeated row at month × region × product line), the null
revenue concentrated in one segment, the negative COGS credit, the P&L and ledger overlap with every total
reconciled (COGS −0.14%), and the month foreign keys to staffing.

Acceptance scenarios covered: **A** (catalog intelligence), **B** (metric lineage), **D** for a changed schema
(an expired credential is PR B), **F** (dependency change, with a dashboard row inserted directly because building a
real dashboard in a unit test is slow; the impact walk reads the same table the Studio writes), **G** for data and
definitions (search across workflows and documents; collaboration permissions are PR D).

## PR B: connector maturity (#29, merged)

Built in `src/lyst/connectors/reliability.py`, `src/lyst/connectors/__init__.py` and `src/lyst/notices.py`;
control migration 13 (`connection_events`; health columns on `connections`; `connection_syncs.last_schema`); tests
in `tests/test_connector_ops.py`; operations described in `docs/CONNECTORS.md`.

* Normalized error categories with a required action each; connectors that know the cause say so (HTTP 401, 403,
  404, 429 with `Retry-After`, 5xx), PostgreSQL connection messages are recognized (an unknown role is an auth
  failure, an unknown database a configuration error; checked against the local PostgreSQL 17).
* One retry policy for transient failures across every connector, with backoff and `Retry-After`.
* An event per operation and a health record per connection (state, reasons, action, credential state, per-table
  freshness, schedules, overdue syncs, contract outcomes, last-day counts), over REST.
* Inbox notices for connections that need a person, deduplicated per cause per day (shared with data contracts
  through `notices.raise_notice`).
* Schema changes detected on every sync; a retyped column on an incremental sync reloads in full.
* Manifests declare `auth`, `read_only` and `retry_attempts`.

Verified: 307 tests pass (19 new), ruff, eval and the license check. Acceptance scenario **D** runs in full: an
expired token (the source starts answering 401) puts the connection in `needs_action` with the reconnect action and
an inbox notice, nothing is retried, no secret appears in health or events, and replacing the credential in Settings
recovers the next sync; a source that retypes a column and adds one triggers a full reload and a breaking contract
check, and health points at the contract.

Not built: connectors for MySQL, SQL Server, warehouses and SaaS systems (no servers to test against, see
`docs/CONNECTORS.md`); generated connectors with sandbox tests and approved promotion (G13 below); webhook-driven
(push) syncs.

## PR C: artifact intelligence and lifecycle (#34, merged)

Built in `src/lyst/assets.py` with hooks in `deliverables.py`, `studio.py`, `workflows.py`, `semantic` and the
runtime's worker tick; workspace migration 10 (`assets`, `asset_versions`); tests in `tests/test_assets.py`.

* **One registry** for reports (PDF), workbooks, decks, briefings, dashboards and compiled workflows: identity, owner,
  visibility, version, validation status (from the analysis's own verification or the dashboard's checks), the task
  and question that produced it, and its dependencies (dataset versions and metric definition versions).
* **Every version kept**: rebuilt reports are new files beside the old; a dashboard's previous folder is copied to
  `<folder>.versions/v<n>` before a refresh overwrites it; `asset_versions` records hash, dependencies and validation.
* **Maintenance**: after every ingest and every definition change, each active asset is assessed as current, stale
  (newer data), invalidated (a metric it uses was redefined or retired; also a stale workflow) or broken (a dataset
  gone, a contract breach, a failed dashboard). Invalidated and broken assets notify their owner once; a contract
  breach is not repeated per asset because its own notice lists them.
* **Repair**: a proposal per asset (refresh a dashboard; re-run and rebuild a report; re-run and recompile a workflow;
  or "needs a person" when the cause is upstream). Repairs are verified like the original and only a verified result
  becomes a new version; a failed repair restores the earlier state with the reason. Repairs started as tasks finish
  on the worker tick.
* **Cross-artifact**: `catalog.impact_of` (used by contracts and lineage) now includes registered reports, so a
  source change names the reports built on it.
* REST: `/v1/workspaces/{ws}/assets` (filters `kind`, `health`, `retired`), `/assets/{id}`, `/assets/scan`,
  `/assets/{id}/proposal`, `/assets/{id}/repair`, `/assets/{id}/retire`.

Verified: 315 tests pass (6 new), ruff, eval 19/19. Scenario **F** end to end: new data marks a report stale without
a notice, a redefined metric invalidates it with a notice, a breaking source version marks it broken with a
"needs a person" proposal; scenario **C** for a dashboard: built, refreshed as version 2 with its checks passing, old
folder kept, history inspected, retired. A stale report rebuilt through a real task becomes version 2 with the new
dataset version, and version 1's file is untouched.

Not built: polished output standards beyond the existing PDF, Excel and slide builders (citations are carried; no
template review); editing an asset in place; repairs that rewrite a broken definition (they stop and ask).

## PR D: team collaboration (#36, merged)

Built in `src/lyst/collab.py` (with `notices.py` honouring preferences and asset shares in `assets.py`); workspace
migration 11 (`comments`, `assignments`, `reviews`, `asset_shares`, `user_notifications`, `notification_prefs`);
tests in `tests/test_collab.py`. No new roles or administration screens: it uses the existing viewer, editor,
admin and owner roles.

* **Discussion on any object** (an investigation task, an analysis, an asset, a metric, a dataset): comments,
  replies, `@email` mentions, **change requests** (open until the requester, the owner or an admin resolves them) and
  **decisions** (recorded by editors and up, listed across the workspace). A private asset's thread is as private as
  the asset.
* **Assignments**: hand an object to a member with a note and a due date; completing it tells the assigner.
* **Reviews that gate shared changes**: an editor proposes a metric definition, a data contract or the publication
  of a private asset; an admin who is not the proposer approves (the change is then applied as the approver, with
  the review id in the metric history and the audit) or asks for changes (nothing applied). A plain "please look"
  review goes to a named member.
* **Sharing and ownership**: assets can be private, shared with named members, or visible to the workspace; owners
  and admins hand ownership of assets, datasets and metrics to an editor or above.
* **Personal notifications**: mentions, assignments, completed assignments, review requests and decisions, comments
  on what you own, merged with platform notices (contracts, connections, assets) in one list with read state;
  each kind can be switched off per person, and platform notices honour the same switches.
* REST under `/v1/workspaces/{ws}/objects/{kind}/{id}/comments|activity`, `/comments/{id}/resolve`, `/decisions`,
  `/assignments`, `/reviews`, `/assets/{id}/share`, `/ownership`, `/notifications`, `/notification-preferences`.

Verified: 320 tests pass (5 new). Scenario **E** end to end: Avery shares a private report with Sam (a viewer cannot
see it or its thread), assigns it, Sam requests a change mentioning Avery, Avery resolves it and records a decision,
Sam completes the assignment, ownership moves to Sam, and every step is in the audit log. Metric and contract
changes proposed by an editor are applied only after an admin other than the proposer approves.

Not built: email or chat delivery of personal notifications (the inbox and the existing workspace webhook only),
digests, team or organizational workspaces above a workspace (a workspace is the team today), and real-time
co-editing.

## PR E: verified transformations (gap G4)

Built in `src/lyst/transforms.py`, with `data_model` assets in `assets.py`; workspace migration 12 (`transforms`,
`transform_versions`, `transform_runs`); tests in `tests/test_transforms.py`.

* A transformation is a named `SELECT` over governed datasets, run through the same SQL guard, whose output is an
  ordinary governed dataset (versions, insights, contracts, lineage `derived_from` each input, a `data_model` asset).
* **Verified before saving** by running it: not empty, a declared key unique, no join fan-out, columns not mostly
  empty, summed measures reconcile with their single input. Advice for `SELECT *`, `ORDER BY` without `LIMIT`,
  `DISTINCT`, joins without a condition. `explain` reads inputs, joins, filter, grouping and computed columns from
  the SQL.
* **Designed from a sentence** when a model is configured: the model sees dataset names, column names and types,
  grain and relationships, never rows; its SQL is previewed and returned as a proposal. Without a model Lyst says so
  and takes SQL.
* **Follows its inputs**: an input's new version re-runs it (chains in order, no cycles); `incremental_column`
  appends only newer rows. A failed run keeps the last good version, marks the transformation failed with the reason,
  and its asset reads broken with "needs a person"; a new definition version recovers it.
* A derived dataset takes the strictest classification of its inputs (public < internal < confidential < restricted; unknown counts as internal); a level raised by hand stays.
* Refused: a key that repeats, a join that multiplies rows, a name that is already an ordinary dataset, cycles, and
  definitions by someone who sees only some rows of an input (the result would be silently partial).
* REST: `/v1/workspaces/{ws}/transforms` (list, define), `/transforms/design`, `/transforms/preview`,
  `/transforms/{name}` (definition, versions, runs, advice, explain), `/transforms/{name}/run`, `/transforms/{name}/retire`.

Verified: 332 tests pass (7 new), ruff, eval 19/19. On the demo ledger the preview catches the planted double booking
as a repeated key, a month-only join as a fan-out, and reconciles summed COGS to the cent.

Not built: Polars/Python pipelines (SQL only; the skill sandbox is the path for Python), and transformations
scheduled on their own (they follow their inputs and can be run on demand).

## Where it surfaces (handed to the UX thread)

The API is complete; the screens are the UX thread's call. Proposed placement, following their layout:
catalog rows, insights and contracts in **Data › Datasets** (a dataset dialog with Summary · Detail · Method tabs:
summary sentence and risks; columns and relationships; contract and check history); metric lineage, history,
deprecation and the semantic audit in **Brain › Definitions**; search in the single box on Home or the sidebar;
contract findings already arrive in the existing inbox. Collaboration goes on the answer act bar and in Work (comments, assign, request review on an investigation),
with a notifications list and preferences under the person's menu, and pending reviews for admins. Transformations go in **Data › Datasets** as "Build a dataset" (describe or write SQL, preview with checks, save).
Assets go in **Data › Dashboards** and **Saved analyses** (a health
badge, the reason, the proposal and a Repair button, and the version list). Connection health and events go in **Data › Datasets ›
Sources** (a state badge, the reasons and the required action per connection, the event list on open).

## Prioritized record of remaining gaps

Ranked by user impact, completeness, reuse, reliability, security, dependencies, simplicity and measurable benefit.
"Next" items are scheduled in this iteration in Rohn's order.

| # | Gap | Priority | Status |
|---|---|---|---|
| G1 | Connector health and diagnostics, shared retries and rate limits, schema evolution on sync | 1 | done (PR B) |
| G2 | Unified artifact registry with versions, dependencies, health and verified repair | 1 | done (PR C) |
| G15 | Output standards review: house templates, citation formatting and an automated layout check for PDF and slides | 2 | open |
| G3 | Collaboration: comments, change requests, decisions, assignments, reviews gating metric and contract changes, sharing, ownership, notification preferences | 1 | done (PR D) |
| G16 | Personal notifications by email or chat, and digests | 2 | open |
| G17 | Team and organizational spaces above a workspace (shared definitions across workspaces) | 3 | open |
| G4 | Natural-language transformations and verified derived datasets (SQL) | 2 | done (PR E); Polars/Python pipelines open |
| G5 | Fiscal calendars: periods such as "last fiscal quarter" resolved by a workspace calendar; today metric periods are calendar periods only | 2 | open |
| G6 | Source precedence as a rule (which of two overlapping sources answers), not only an audit finding | 2 | open |
| G7 | Analyst answers to lineage questions in plain words from the Home box ("where does operating margin come from?"); today the answer is the API and MCP lineage tool | 2 | open |
| G8 | Workflow composition with branches, approval gates and event triggers; natural-language schedules | 3 | open |
| G9 | Platform health page (workers, queues, DuckDB, storage, backups, connections) | 3 | open |
| G10 | Extension manifest beyond skills and connectors | 3 | open |
| G11 | Embedding-based search across catalog items (documents already have it) | 3 | open |
| G13 | Generated connectors: Lyst drafts an adapter from an API description, tests it in the skill sandbox, and an admin promotes it | 2 | open |
| G14 | Connectors for MySQL, SQL Server, Snowflake, Databricks, Redshift, object storage and SaaS systems | 2 | waiting (servers to test against) |
| G12 | Insights on very large tables run exact distinct counts for keys; fine to tens of millions of rows on one machine, not measured beyond that | 3 | open |

## Where each workstream of the prompt stands after this iteration

| Workstream | State |
|---|---|
| 1 Data intelligence | Discovery, profiling, relationships, contracts done (PR A); verified SQL transformations designed from a sentence done (PR E); Python pipelines open |
| 2 Integrations | Uniform operations, retries, health, schema evolution done (PR B); new connectors wait on test servers (G14); generated connectors open (G13) |
| 3 Knowledge and semantics | Semantic audit, lifecycle, history, lineage done (PR A); fiscal calendars and source precedence open (G5, G6) |
| 4 Automation | Unchanged this iteration: approval-gated schedules, monitors, compiled workflows; composition with branches and gates open (G8) |
| 5 Artifacts | Registry, health, verified repair done (PR C); output-standards review open (G15) |
| 6 Collaboration | Done for a workspace (PR D); email/chat delivery and cross-workspace spaces open (G16, G17) |
| 7 Search | Catalog search across data, definitions, saved work and documents done (PR A); embeddings for catalog items open (G11) |
| 8 Personalization | Notification preferences only (PR D); the rest is open |
| 9 Administration | Unchanged this iteration |
| 10 Platform health | Connection health done (PR B); a single platform health page open (G9) |
| 11 Extensibility | Connector manifests now declare auth and retries (PR B); a general extension manifest open (G10) |
| 12 Cross-system | Overlapping sources reconciled by totals (PR A); reusable reconciliation and entity matching open |
| 13 Acceptance scenarios | A, B, C (dashboard), D, E, F, G pass in tests; H (an SDK extension end to end) is covered by the existing connector and skill tests, not re-run as one scenario |
