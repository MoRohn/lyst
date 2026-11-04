# Enterprise adaptability

This register covers the ninth iteration: how Lyst enters a new organization, learns its approved business
context, explains its results, and stays governed as the environment changes. It records what is built and
verified, what is partial, and what is still open. Sibling registers: `PLATFORM.md` (catalog, contracts,
connectors, assets), `ENGINEERING_EXCELLENCE.md`, `PRODUCT_EXCELLENCE.md`, `UX_STRATEGY.md`.

## 1. Onboarding and business discovery

**Built and verified** (`src/lyst/onboarding.py`, `src/lyst/domains/`, `tests/test_onboarding.py`):

- **Domain packages.** Thirteen versioned packages (finance and FP&A, procurement, sales, marketing, workforce,
  manufacturing, customer service, product, logistics, IT operations, healthcare operations, professional
  services, financial services) in `src/lyst/domains/packages/*.json`. Each holds signals, concepts, candidate
  metrics described by the column names they are usually built from, starter questions, usual sources and
  glossary terms. They are starting points, never authority.
- **Recognition from metadata only.** Dataset names, titles, descriptions, column names, catalog semantic types
  and document titles; never rows. Each recognized domain carries its evidence (which dataset or column matched
  which signal) and the usual sources that seem to be missing.
- **Progressive discovery.** `POST /v1/workspaces/{ws}/onboarding/discover` runs the catalog's discovery, then
  proposes: metrics (matched to a numeric column, or an identifier for distinct counts, with the dataset's date
  column and small categorical dimensions), glossary terms, column policies for personal data, data contracts
  drafted from each dataset's current shape, starter questions, and sources to connect. Discovery is repeatable:
  accepted and dismissed proposals stay decided.
- **Proposed versus approved.** A proposal is marked `authority: proposed` until an administrator accepts it.
  Accepting goes through the governed service it belongs to (`SemanticService.define`, `set_term`,
  `DataService.set_column_policy`, `ContractService.define`, `TaskRuntime.submit`), so the same permission checks,
  versioning and audit apply, and the metric version's note records the proposal and package version.
- **Versioned business profile.** `GET/PUT /v1/workspaces/{ws}/profile` (history with `?history=true`): adopted
  domain packages pinned to their versions, the organization's own terms (each must map to an approved metric and
  becomes a glossary term, so questions in the organization's words resolve to its definitions), fiscal year
  start, reporting period and objectives.
- **Guided steps and first-value milestones.** `GET /v1/workspaces/{ws}/onboarding`: six steps (connect,
  discover, confirm definitions, invite the team, objectives, first answer; two optional), the next required
  step, a state (`in_progress`, `stalled` after seven days without progress, `complete`), and milestones measured
  from existing records: seconds to first dataset, first analysis and first complete analysis, connector
  failures as configuration errors, approvals and decided proposals as interventions, dismissed suggestions.
- **Scenarios.** A: a new procurement and HR organization is recognized, accepts spend, a column policy and a
  contract, and runs a starter question to a finished analysis. B: two organizations from different industries
  keep separate proposals and profiles, and neither can read or accept the other's.

**Open**

- O1. The fiscal calendar is stored in the profile but period parsing (`analyst/periods.py`) still uses calendar
  quarters. Next: pass the profile's fiscal start into `periods.parse`.
- O2. Cross-domain models ("how did procurement, workforce and sales affect margin") are not built: recognition
  can return several domains, but there is no reconciled multi-dataset model yet.
- O3. When the collaboration reviews (`reviews` table, PR #36) land, accepting a metric or contract proposal
  should open a review where the workspace requires a second approver, instead of defining it directly.
- O4. No UI yet. The UX thread owns placement; the API is ready for a "Get started" card on Home and a
  suggestions list under Data.
- O5. Recognition is lexical. An embedding model, where one is allowed, could match columns named in another
  language or by convention.

## 2. Evidence graph and explainable results

**Built and verified** (`src/lyst/evidence.py`, `tests/test_evidence.py`):

- **Evidence graph per answer.** `GET /v1/tasks/{id}/evidence` builds nodes and edges from records the engine
  already keeps: the answer and each finding (claims), every figure stated in them, the step that computed each
  figure, the SQL each step ran, the dataset version each query read (rows, checksum, load time, loader,
  classification), the source it was loaded from, the governed metric definition version in effect when the task
  ran, validation checks, assumptions, cited passages, approvals and model wording. Plain records, no graph
  database.
- **Traced, not guessed.** A figure is traced when a step's stored output holds the same number at the
  precision written (the tolerance the engine already uses to reject a model's unbacked numbers). A figure that
  matches nothing is counted as untraced and listed.
- **Reports trace the same way.** `GET /v1/artifacts/{id}/evidence` links a generated report, workbook or deck to
  the answer it presents (scenario C).
- **Trust questions answered from records.** `GET /v1/tasks/{id}/explain?q=how|sources|method|assumptions|change|reproduce|actions`
  (`&figure=` picks one figure). Each answer has a one-sentence `short` and the `detail` records behind it.
- **Is it current, and can I reproduce it?** The graph reports any dataset with a newer version than the one used.
  `q=reproduce` re-runs every recorded query as the caller, under the caller's current policies, and compares row
  counts; queries over data that has changed are reported as not comparable rather than re-run.

**Open**

- E1. Figures the answer derives inline (a change in points between two traced values) show as untraced. Next:
  mark a figure *derived* when it equals the difference or ratio of two traced figures in the same claim.
- E2. Reproduction compares row counts, not values: the query log does not keep result hashes. Next: store a
  hash of each result in `query_history` and compare it.
- E3. "Why this method" uses each capability's description; the planner's own reasons (intent rules, workflow
  reuse) are in the plan output but not yet surfaced as a sentence.
- E4. No UI yet; the "How this ran" dialog is the natural home for `explain` (UX thread's placement).
