# Product Excellence & Intelligence Register

Kept current with each product-excellence pass. Every entry is backed by something checked: a test, a
measurement, or the running app (`lyst serve` + `lyst ui` on the Northwind demo, driven in a browser).
Nothing here is a plan dressed as a result.

**Pass 1, 2026-11-04**, on `main` at `5c5968c` (after the credential boundary landed). Baseline: `uv run pytest`
170 passed; `lyst eval` 19/19. After this pass: 181 passed (11 new tests); `lyst eval` 19/19.

Scales: **Severity** S1 (wrong or misleading output, data or security risk) to S4 (polish). **Friction** is what a
person feels: High (blocks or misleads), Medium (extra steps, confusion), Low. **Effort**: S, M, L.

## Fixed in this pass

| # | Subsystem | Finding (evidence) | Root cause | Sev | Friction | Fix | Proof |
|---|---|---|---|---|---|---|---|
| R1 | UI, answers | Every answer with two dollar amounts rendered as garbled math: "Revenue was $25.41M in Q3 2026, +$316.0K … ($25.10M)" showed as italic LaTeX (seen in the running app) | `st.markdown` treats `$…$` as LaTeX; model- and data-derived text went in unescaped | S1 | High | One `md()` helper escapes every data-derived string shown as markdown, on all six pages | Running app, before/after |
| R2 | Analyst | A finished answer and its Plotly charts were redrawn every second for as long as the page was open; "Recent work" said *Running* beside an answer marked *Done* | The progress fragment had `run_every=1` and also rendered the result; Recent work sat outside it | S2 | Medium | The fragment polls only while the task is active, then triggers one full rerun; the result renders once | Running app |
| R3 | Workflow compilation | Nothing compiled: every repeat of a verified analysis was re-read (model classification for unmapped terms, a member scan per dimension) and re-planned | No workflow concept existed (`grep -ri workflow src` found none) | S2 | Medium | Workflow compiler (below) | `test_workflows.py` (9 tests), benchmark |
| R4 | Knowledge, consistency | Asking about Q4 2025 after Q3 2026 warned "An earlier analysis on the same data answered differently … review both" (seen in the running app) | The consistency key used search tokens, which drop `q4` and `2025`, so different periods compared equal | S1 | High (false alarm erodes trust) | A question key that keeps period tokens | `test_questions_about_different_periods_are_not_reported_as_contradictions` (fails before the fix) |
| R5 | Memory | Each repeat of a question stored another identical episodic memory (and, with embeddings configured, another embedding call) | `remember` never checked for an existing identical fact | S3 | Low now, grows with use | Same layer, scope, trust and content is kept once | `test_a_repeated_question_does_not_pile_up_duplicate_memories` |
| R6 | Analyst, exports | Exporting took two clicks per format ("Excel", then "Save analysis.xlsx") | `button` then `download_button` | S3 | Medium | One `download_button` per format; the file is built only on click | Running app |
| R7 | Analyst, resumability | Reloading the page or sharing the link lost the open investigation | The task id lived only in session state | S3 | Medium | The open task is kept in the URL (`?task=`); access is still checked by the API | Running app (reload returns to the same answer) |
| R8 | Analyst, approvals and failures | "A step needs approval. An admin can approve it in Work" with no way there; a failed task offered no retry | — | S3 | Medium | Link to Work › Approvals; inline Retry | Code |
| R9 | Analyst, progress | Progress listed raw identifiers ("business analyst · understand") | — | S4 | Low | Plain-language phase and a step count on a progress bar | Running app |
| R10 | Build, dashboards | Every dashboard's zip bundle was downloaded from the API on every rerun of the Build page | `download_button` was given bytes fetched eagerly | S3 | Low (grows with dashboards) | Fetched on click | Code |
| R11 | Design system | Text wordmark "Lyst"; a blue accent unrelated to the brand | — | S4 | Low | The Lyst logo (light and dark variants) via `st.logo`, mark as favicon; brand teal as the single accent at shades that keep white button text at WCAG AA (6.4:1 light, 5.1:1 dark); charts lead with the accent | Running app, light and dark |

## The workflow compiler (Pass 3)

`src/lyst/workflows.py`. When an analysis finishes complete and verified (every error-severity check passed), Lyst
compiles it into a workflow with: purpose; a typed `period` input whose grain (quarter, month, year) is part of
the contract; the resolved understanding (intent, metrics, split, filters, depth); the execution graph with period
arguments as placeholders re-derived per run; data dependencies (dataset schema fingerprints, metric versions);
required permissions; required checks and verification intensity; a step budget; version history and provenance
(source and last verifying task, verified runs, reuse count, failed verifications).

Reuse decision on every request: a verified workflow whose request pattern matches (or that is named directly) and
whose dependencies are current runs as compiled; otherwise Lyst plans afresh. A changed schema or metric version
marks the workflow stale and the next verified run compiles version n+1; a capability registered after compilation
(a matching skill) wins over the older workflow. Compiled runs are validated exactly like planned runs; a compiled
run that fails verification is counted against the workflow and never compiled. Skills, SQL, dashboards,
connector syncs and anything mutating or approval-gated are never compiled. `LYST_WORKFLOWS=0` turns it off.

Surfaces: Analyst says "Ran the verified workflow …" or "saved as a reusable workflow"; Build › Workflows lists
them with status, runs, the plan, checks, dependencies and permissions, and runs one for a period (then opens the
result in Analyst) or retires it. API: `GET/POST /v1/workspaces/{ws}/workflows[/{id}[/run|/retire]]`.

### Measured (scenario B and D)

`scripts/bench_workflows.py`: a fresh demo workspace, a stub model on a local endpoint (so model calls are
counted, not estimated), three analyses asked for four quarters (12 tasks), compilation off then on.

| Repeat periods (9 tasks) | Compilation off | Compilation on | Change |
|---|---:|---:|---:|
| Model calls | 12 | 9 | −25% |
| SQL queries | 102 | 84 | −18% |
| Verified answers | 9 / 9 | 9 / 9 | same |
| Median task time (no model latency) | 0.104 s | 0.099 s | within noise |

The first period is identical in both runs (4 model calls, 34 queries), as it must be. What the numbers say: a
repeat no longer needs the model to read the request, nor a member scan per dimension; the answers, evidence,
tables, charts and checks are identical to a planned run (asserted in `test_verified_analysis_compiles_…`). What
they do not say: wall time excludes real model latency (each avoided call is typically 1 to 10 s on a hosted
model); the narrative call is kept on compiled runs by design, because wording is judgment.

## User journeys checked in the running app

| Journey | Result |
|---|---|
| First request from a suggestion | One click to a verified answer with evidence, chart, implications, actions; progress shown in plain language |
| Inspect a result | Supporting work expander: sources with versions and hashes, assumptions, checks, plan, every query |
| Repeat an analysis for another period | Typed in Analyst: reused the compiled workflow, said so, same verified checks |
| Run a workflow from Build | Period typed, Run: opened in Analyst with the verified result (Build → Analyst continuity) |
| Reload or share | The open task survives a reload through the URL |
| Light and dark | Logo variant and accent correct in both |

## Open register (found, not yet fixed), in priority order

| # | Subsystem | Finding | Sev | Effort | Next step |
|---|---|---|---|---|---|
| O1 | Runtime, UI | A task whose answer "needs your input" (ambiguous metric) is stored as `done`, so Work's board and Recent work show *Done* | S2 | S | A distinct terminal state or a result-aware badge |
| O2 | Scheduling | Schedules re-ask the question text each time; they could run the compiled workflow for the next period and skip re-reading | S2 | M | Schedule a workflow by id with a rolling period |
| O3 | Work page | All four tabs (board, approvals, schedules, monitors with findings) fetch on every rerun, and every button rerun refetches all of them | S3 | M | Fragments per tab, short-TTL caching in the client |
| O4 | Observability | `opentelemetry.context: Failed to detach context` is logged during the suite (a span context crosses threads) | S3 | S | Attach/detach the OTel context on the same thread in the step runner and API middleware |
| O5 | CLI | ~~`lyst eval` stopped in setup mode without `LYST_MASTER_KEY`~~ Closed: fixed upstream in 3faf202 (ephemeral key for the evaluation home); verified with no key, 19/19 | — | — | — |
| O6 | Understanding | Intents come from regular expressions; a model is consulted only when no metric matches. A compiled purpose keeps the original verb ("decline") even when the new period rose (the answer itself is correct) | S3 | M | Neutral purpose wording; model-assisted intent with the approved-names contract for all unmatched phrasings |
| O7 | Adaptive presentation | Audience is executive or analyst by keyword only; no control to change depth or format without re-asking | S3 | M | An audience control on the answer (business, analyst, engineer, executive) that re-renders the same result |
| O8 | Efficiency | The narrative call is repeated on compiled runs even when findings are identical to a previous run | S4 | S | Cache narrative by findings hash |
| O9 | Models | No real OpenRouter or local model has been exercised end to end (no key on this machine) | S2 | S | One live run once a key is stored in Settings |
| O10 | DuckDB | One engine process owns each file (by design); no multi-process writer or read replicas | S3 | L | Documented in DUCKDB.md; revisit for multi-node |

Scenario coverage after this pass: A, C (adversarial), G (resume) and H (isolation) by existing tests; B and D by
`test_workflows.py` and the benchmark; E (cross-product continuity) partly, Build › Workflows → Analyst, checked
in the running app; F (first-time user) by the journey table above, not yet with a person.


## Pass 2, 2026-11-04: open items, adversarial data, and the interface revamp

Baseline 188 tests on `main` at `2143664`; after this pass 219 (31 new). `lyst eval` 19/19.

### Closed from the open register

| # | Finding | Fix | Proof |
|---|---|---|---|
| O1 | A task whose answer needs the person's input was shown as *Done* | Tasks carry an `outcome` (complete, needs input, not verified); every list shows it; Work and Home list them under *Needs you* | `test_a_finished_task_reports_its_outcome…` |
| O2 | Schedules re-asked the question text | A schedule can run a compiled workflow on a rolling period and plans afresh if it went stale | `test_a_scheduled_workflow_runs_compiled…` |
| O3 | Work fetched and drew all tabs on every rerun | Three views; only the one in view is fetched, each reruns alone | Running app |
| O4 | `Failed to detach context` logged on every suite run | Spans always closed; abandoned requests end their span without detaching in a foreign context | 0 occurrences in the suite (was 1 per run) |
| O7 | Depth and format fixed by keywords | One answer, four depths (Business, Analyst, Engineer, Executive), switched without re-running and remembered; caveats visible at every depth | `test_ui_answer.py` |
| O8 | The model reworded identical findings on every run | Identical answer and findings reuse the earlier verified wording | `test_identical_findings_reuse…` |

### New findings, found by probing, and fixed (scenario C)

| Finding (before) | Sev | Now | Proof |
|---|---|---|---|
| Six duplicated rows made Q3 revenue 24% higher and the answer said **verified** | S1 | `duplicate_rows` error inside the analysed period; the answer leads with "Not verified: 6 of 42 rows…" | `test_adversarial_data.py` |
| EUR and USD amounts were summed and **verified** | S1 | `single_currency` error unless filtered to one currency | same |
| A quarter with two of three months showed a 31.5% "decline" with only a warning, and "why did it decline" accepted the premise | S1 | Incomplete current or prior period is an error unless the person asked period-to-date | same |
| Data starting inside the prior period compared a partial with a full period | S1 | `period_start:*` checks | same |
| A dropped metric column crashed the task with a raw database error | S2 | A plain blocker naming the column and the dataset version | same |
| A corrupt upload returned a server error | S3 | HTTP 400 with what to fix; nothing loaded | same |
| "Why did margin decline" when it rose was answered as if it had declined | S1 | The answer says first that it rose | `test_premise.py` |
| A reworded or narrated answer was reported as contradicting an earlier one | S2 | Consistency compares figures, not wording | `test_rewording_is_not_a_contradiction…` |

### Interface revamp

Rohn's verdict on the interface was that it was fragmented and not user-friendly. What changed:

* **One navigation.** The six areas live in the sidebar with the logo, the workspace and an account menu; Work shows how
  many things wait on you.
* **One design kit** (`src/lyst/ui/kit.py`): page header with one primary action, at most three sections per page (kept
  in the URL), list rows with status chips instead of raw tables, empty states that say what to do, toasts for every
  action, readable times ("26 min ago"), and one stylesheet that also keeps rows on one line on a phone.
* **Analyst is home**: the ask box, suggestions generated from the workspace's own metrics, what Lyst can use, *Needs you*
  (approvals, unread findings, answers waiting on you) and *Continue where you left off*. Follow-ups sit under the answer.
* **Work** = Needs you · Activity · Recurring (schedules, monitors, findings); a task's steps open in a dialog.
  **Data** = Datasets · Connections · SQL with one *Add data* dialog (upload or connect; the dataset name is derived from
  the file). **Intelligence** = Definitions · Documents · Memory. **Build** = Workflows · Dashboards · Skills, with a
  *New dashboard* form instead of a sentence you had to know.

Measured by walking each journey in the running app (clicks after arriving at Lyst):

| Journey | Before | After |
|---|---:|---:|
| Approve a waiting step | 3, and nothing told you one was waiting | 2, with a count in the navigation and on Home |
| Add a CSV as a dataset | 5 (tab, file, type a name, add) | 4 (name filled in) |
| Build a dashboard | Only by typing the exact phrasing | 4 (New dashboard, metrics, split, Build it) |
| Schedule a recurring analysis on a verified workflow | Not possible | 3 |
| Reopen earlier work | Scroll, Open | 1 from Home |
| Export a briefing | 2 (pass 1 made it 1) | 1 |
| Sub-views across the five work areas | 21 tabs | 12 sections + Home |

Every page is rendered headlessly against the in-process API in `tests/test_ui_pages.py` (no page may raise); light,
dark and phone widths were checked in a browser. Limitations: Streamlit decides much of the chrome (the sidebar toggle,
dialog sizes); Settings keeps the build thread's layout under the shared header; no usability session with a first-time
user has been run yet (scenario F).


## Pass 6, 2026-11-04: refinement and robustness

Baseline on `main` at `3dfe728` (after #14, #15, #16): 260 tests. After this pass: 270 passed (10 new, in
`tests/test_refine.py`); after the second part (P6-7, P6-8), 272; `lyst eval` 19/19; ruff clean. Live checks ran on a separate instance (engine :8890, interface
:8610, its own home and key), never on the instance at :8770. Scenarios F (concurrent workspaces) and G (credential
failure) were covered by the audit thread in #15 and are not repeated here.

Classes: Critical Defect · Functional Gap · Reliability Risk · Performance Issue · UX Friction · Architectural Debt ·
Enhancement Opportunity. Status: Verified Fixed · Implemented/Unverified · Partially Resolved · Open · Not Evaluated.

| # | Class | Finding (evidence) | Root cause | Fix | Resolution criterion | Status |
|---|---|---|---|---|---|---|
| P6-1 | Reliability Risk | The same submission sent twice queued two tasks: two concurrent `POST /tasks` with one `Idempotency-Key` returned `t_5b2ecb1c4a74` and `t_4feac9cd36c0` (live) | Submission had no idempotency; nothing tied a retry or double click to the first request | `Idempotency-Key` honoured per person and workspace; check and insert in one write transaction on the writer thread (control migration 11). The interface sends a key per request (session, workspace, question, 30 s window) from Home, Data, Intelligence and Build | 8 concurrent repeats give 1 task; another person's identical key never returns someone else's task; live repeat returned the same id | Verified Fixed (`test_a_repeated_submission…`, `test_an_idempotency_key_never…`, live) |
| P6-2 | Reliability Risk | Scenario C live: 40 tasks, engine killed with SIGKILL 0.6 s in, restarted. All 40 finished and no audit, memory or step was duplicated, but the two tasks that were running sat idle for their full 30 s lease after the engine was back (claimed 10:51:01, re-claimed 10:51:31) | A lease from the previous process was treated like a live worker's | `recover_orphans()` at engine start expires leases held by any other worker id: DuckDB's file lock means no other live engine can hold them. Takeovers are still counted, so the crash-loop limit (R3) still applies | Re-run live: both interrupted tasks re-claimed 0.13 s after restart and done 0.26 s later; 40/40 complete; no duplicate audit `(action, target)` | Verified Fixed (`test_engine_restart_resumes…`, live re-run) |
| P6-3 | Functional Gap | A slow or costly answer could not be traced to a step, a model call or a query (ENGINEERING O1: usage only aggregated per model) | Per-call records (`llm_calls`, `query_history`) existed but were never joined to a task | `GET /v1/tasks/{id}/trace` (same access as reading the task): steps with durations, each model call (purpose, model, level, outcome, latency, tokens, cost) and query (SQL, rows, ms, outcome), totals, slowest step. A call or query is matched to a step only when exactly one step was running then; otherwise it is left unattributed, not guessed. **How this ran** on every answer, including failed and stopped tasks | Totals equal the sum of the records; another workspace gets 403/404 with no SQL in the body | Verified Fixed (`test_trace_…` ×2, UI test, live) |
| P6-4 | UX Friction | Settings was the one page still on the old layout: four large metric tiles, a Python dict on screen (`waiting {'interactive': 0, 'background': 0}`), members and audit as raw tables with user ids and ISO timestamps, a webhook field whose Save button appeared only after editing | Kept the pre-revamp layout under the shared header | Three sections, kept in the URL: **Models** (the tiers thread's block moved unchanged, the one-field Connect OpenRouter form kept), **Connections** (owner only; masked entry, Test/Replace/Revoke/History unchanged), **Workspace** (autonomy with a one-line meaning, webhook form with Save, members as rows with a role chip and Remove, audit trail with readable times and emails). Role, encryption and model calls moved into the header line; the installation tiles became one status line (a warning when unhealthy) | No `metric` elements, no Python dicts, no ISO stamps; light, dark and phone screenshots | Verified Fixed (`test_settings_sections…` ×3, `test_workspace_section…`, screenshots) |
| P6-5 | UX Friction | Empty-state titles sat 12 px left of centre (measured: text centre 849 px, box centre 861 px) | Streamlit's hidden heading-link element takes space after the text | Hidden inside empty states | Re-measured: text centre 860.6 px = box centre | Verified Fixed (browser measurement) |
| P6-6 | Reliability Risk | `learn` writes the analysis row, then the episodic memory, in two transactions; a crash between them leaves the analysis without its memory, and the resumed step returns early with `memory_id` None | Idempotency check keys on the analysis row only | Not changed in this pass (the build thread owns `analyst/engine.py`) | A crash between the two writes still yields one analysis and one memory | Open |
| P6-7 | Enhancement Opportunity | Steps in one wave run in parallel, so their queries could not be attributed (live trace: the three `metric_value` steps showed 0 queries) | Query and call records carried the task id but not the step | The running step's sequence is a context variable set by the runtime; `query_history.step` (workspace migration 8) and `llm_calls.step` (control migration 12) record it; the trace uses it and falls back to timing only for older records | Every query of a task is tied to its step | Verified Fixed (`test_every_query_records_the_step…`) |
| P6-8 | Reliability Risk | A step that never returns held its task as running and its worker busy forever: the heartbeat renewed the lease, and the time budget was only checked between waves. Measured: a 1 s budget held the worker for 6.16 s of a 6 s step | Budget checked before each wave, not while it ran | The wave waits at most the remaining budget; on overrun the late steps are marked `timeout`, the task fails through the fenced `_fail` (a concurrent cancel still wins), the heartbeat stops, and the worker returns at once. A late step's checkpoint is refused once the task is no longer running | A hung step fails its task within the budget (test: under 3 s for a 1 s budget) and its later output never lands | Verified Fixed (`test_a_hung_step_cannot_hold…`) |
| P6-9 | Reliability Risk | A Python step thread cannot be killed, so an overrun step keeps running in the background after its task fails. Its checkpoint is fenced, but an external side effect it makes still relies on the task-id idempotency keys | Threads, not processes, run steps | Steps with side effects already key writes on the task id; a process-per-step model would be needed for hard kills | — | Partially Resolved |
| P6-10 | Reliability Risk | The time budget restarts on every resume (each run measures from its own start), so a task interrupted repeatedly can exceed its budget in total | Elapsed time not persisted | Persist active run time on the task | Budget counts across resumes | Open |

### Screens checked (Playwright, 6 pages × desktop 1440, wide 1920, tablet 820, phone 390 in light, desktop and phone in dark)

36 rendered views before and after: no horizontal scroll at any width, no Streamlit exception, no page error. The only
console errors are Streamlit's own probes of `/<page>/_stcore/health` and `/_stcore/host-config` on a deep link (404,
harmless, Streamlit behaviour). The Settings sections and the How this ran dialog were captured in light, dark and phone.

### Not evaluated in this pass

A first-time user session with a person (scenario H); live model calls in the trace (no key on this instance; the trace
reads the same `llm_calls` rows the gateway writes, exercised in the test by a recorded call); scenario A across
several datasets.
