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

## 3. Classification and policy-aware execution

**Built and verified** (`src/lyst/governance.py`, `tests/test_governance.py`):

- **Four levels and three tags.** public, internal, confidential, restricted; tags personal, financial,
  regulated. Only administrators classify, with a reason, and every change is kept
  (`classification_events`). An unknown legacy label is treated as internal, never as public. Discovery only
  *suggests* a level, from the catalog's measurements (personal-data columns suggest confidential plus the
  personal tag).
- **A versioned data policy per workspace** (`data_policies`). For each level, the lowest role that may read it
  (default: restricted needs admin). The most sensitive level whose results may go to an external model
  (default confidential: aggregates and definitions only, never rows; restricted never). The most sensitive
  level whose results may be exported as files (default confidential). Roles must rise with sensitivity.
- **Enforced in the services, not the prompt.**
  - `DataService.query` calls `check_read` before running. A refusal names the dataset, its level and who to
    ask. It is audited with the rule (`policy.read`), and the query log records it with the datasets involved.
  - `LLMClient.chat` calls `model_gate`. If the task has read data above the external-model ceiling, nothing is
    sent, the answer is worded by Lyst's templates, and a `policy` event on the task says why ("How this ran"
    and `explain?q=actions` show it).
  - `ArtifactService.build` calls `check_export`, so no file is written from results above the export ceiling.
- **The query log knows what each task read.** `query_history.datasets` (workspace migration 31) is how a task's
  sensitivity is known before anything leaves.
- **Policy diagnostics.** `POST /v1/workspaces/{ws}/policy/check` answers ahead of time whether work over given
  datasets or metrics would be allowed, narrowed (hidden columns, row filters, no external model) or refused,
  for `analyze`, `export` or `external_model`. Members get the outcome and who to ask. Administrators also get
  the rule, the levels and the hidden column names.
- **Scenario D.** A viewer querying restricted salaries is refused in the data service with a plain
  explanation. An analysis over it fails with the same reason, and other data is unaffected. For the owner,
  restricted results never reach an external model or a file, while confidential results still do under the
  defaults.

**Open**

- G1. Retention per level (how long query results, artifacts and memory derived from restricted data are kept) is
  not enforced yet; the storage roll-ups and backups threads own retention mechanics.
- G2. Masking (showing a hashed or partial value instead of hiding a column) is not built. Column policies hide
  or show.
- G3. Network destinations (webhooks, HTTP connectors) are not yet checked against the classification of what
  they would carry.
- G4. Embeddings for document search are still governed by the workspace's external-model switch, not by
  document classification.
- G5. No UI yet: per the UX thread, classification belongs on the dataset dialog (Data) and the data policy in
  Settings › Workspace.

## 4. Approval summaries and continuity

**Built and verified** (`src/lyst/standing.py`, `tests/test_standing.py`):

- **Every pending decision explained.** `GET /v1/workspaces/{ws}/approvals/explained` merges the runtime's
  approvals (schedules, monitors, skills, gated steps, decisions) with the collaboration reviews (metric and
  contract definitions, publishing). Each item gives the proposed action, the reason, what it affects (the
  schedule and its cadence, the metric, and every report or dashboard built on a changed definition), the
  consequences, the expected outcome, how to undo it, how long it has waited, and whether the caller can decide.
- **Risk follows what the action does.** Reading only is low. A recurring run is medium. Changing an
  authoritative definition or adding code is high, while a brand-new metric is medium because nothing existing
  changes. A gated step that only reads is low. Items the caller can decide come first, riskiest first.
- **Where things stand.** `GET /v1/workspaces/{ws}/standing?days=7` returns a one-line headline and five lists:
  what the caller asked for, what finished (with not-verified or needs-input flagged), what is running or paused,
  what needs them (tasks waiting for approval, failed, or needing input; approvals they can decide;
  assignments; reports and dashboards that no longer match the data; failing connections), and what happens next
  (the next set-up step, upcoming scheduled runs).

**Open**

- C1. Approval *requirements* still follow each capability's own flag and the workspace autonomy. Risk is
  explained but does not yet decide whether to ask. Next: let read-only steps proceed in `delegate` mode and
  require approval for mutating ones, after agreeing the runtime change with the decision-intelligence thread.
- C2. Task modes (assist, investigate, build, automate, monitor) are not separate settings. Today they map onto
  existing paths: ask, the investigate depth, dashboard building, schedules and monitors.
- C3. Corrections to assumptions and data-source choices are captured only as memory and collaboration comments.
  There is no scoped "correction" record yet that later analyses apply automatically.

## 5. Cost and value attribution

**Built and verified** (`src/lyst/costs.py`, `tests/test_costs.py`; administrators, `audit.read`):

- **Costs.** `GET /v1/workspaces/{ws}/costs?days=30&by=user|task|schedule|model|purpose|workflow` covers:
  - model calls, failures and retries, with retry overhead in dollars;
  - cost (provider-reported, or catalog-priced) and tokens;
  - external calls and model time;
  - tasks, steps and compute time;
  - queries, query time, rows processed and failed queries;
  - connector operations and attempts (external API use);
  - database and artifact bytes on disk.
- **Value, counted rather than claimed.** `GET /v1/workspaces/{ws}/value` counts verified answers, finished and
  failed tasks, the verification rate, the median time from request to a verified answer, scheduled reports
  run, answers served by a compiled workflow, monitor findings, onboarding suggestions accepted, and active
  reports and dashboards. Effort avoided appears only when an administrator passes
  `manual_minutes_per_answer`, and the assumption is returned with the number. No ROI figure.
- **Grounded recommendations.** `GET /v1/workspaces/{ws}/costs/recommendations` suggests four things, each
  citing the numbers and the tasks or model behind it:
  - schedule a question asked three or more times;
  - route away from a model whose failure rate is 20% or more;
  - pre-aggregate datasets whose 95th-percentile query time is two seconds or more;
  - reuse the verified method for questions planned from scratch three or more times.

**Open**

- V1. Costs are not yet attributed to projects (there is no project object) or to integrations beyond connector
  operation counts. The decision-intelligence thread's quotas read the same `llm_calls`, and these numbers
  should agree with its usage view.
- V2. User acceptance (thumbs up or down on answers) is not recorded anywhere yet, so it is not counted.
