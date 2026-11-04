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

## PR A: data intelligence and semantic modeling (this PR)

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

## Where it surfaces (handed to the UX thread)

The API is complete; the screens are the UX thread's call. Proposed placement, following their layout:
catalog rows, insights and contracts in **Data › Datasets** (a dataset dialog with Summary · Detail · Method tabs:
summary sentence and risks; columns and relationships; contract and check history); metric lineage, history,
deprecation and the semantic audit in **Brain › Definitions**; search in the single box on Home or the sidebar;
contract findings already arrive in the existing inbox.

## Prioritized record of remaining gaps

Ranked by user impact, completeness, reuse, reliability, security, dependencies, simplicity and measurable benefit.
"Next" items are scheduled in this iteration in Rohn's order.

| # | Gap | Priority | Status |
|---|---|---|---|
| G1 | Connector health and diagnostics: per connection last success, last failure class (auth, permission, network, schema, rate limit), required action, freshness; shared retry and rate-limit policy; schema evolution on sync feeding contracts | 1 | next (PR B) |
| G2 | Unified artifact registry: one identity, owner, version, validation status and dependencies for reports, decks, workbooks, dashboards, workflows; stale detection when sources or definitions change (builds on `impact`) | 1 | next (PR C) |
| G3 | Collaboration: share an investigation, comments on artifacts, review and approval of metric changes and contracts, ownership transfer, notification preferences | 1 | next (PR D) |
| G4 | Natural-language transformations: derived datasets from a DuckDB SQL or Polars definition, verified by running them and checking the result against a contract, with lineage | 2 | open |
| G5 | Fiscal calendars: periods such as "last fiscal quarter" resolved by a workspace calendar; today metric periods are calendar periods only | 2 | open |
| G6 | Source precedence as a rule (which of two overlapping sources answers), not only an audit finding | 2 | open |
| G7 | Analyst answers to lineage questions in plain words from the Home box ("where does operating margin come from?"); today the answer is the API and MCP lineage tool | 2 | open |
| G8 | Workflow composition with branches, approval gates and event triggers; natural-language schedules | 3 | open |
| G9 | Platform health page (workers, queues, DuckDB, storage, backups, connections) | 3 | open |
| G10 | Extension manifest beyond skills and connectors | 3 | open |
| G11 | Embedding-based search across catalog items (documents already have it) | 3 | open |
| G12 | Insights on very large tables run exact distinct counts for keys; fine to tens of millions of rows on one machine, not measured beyond that | 3 | open |
