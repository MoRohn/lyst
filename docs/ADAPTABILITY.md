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
