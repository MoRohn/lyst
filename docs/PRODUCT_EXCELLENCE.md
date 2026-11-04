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
| O5 | CLI | Since the credential boundary, `lyst eval` stops in setup mode unless `LYST_MASTER_KEY` is set, though it builds a throwaway workspace | S3 | S | Use an ephemeral key for the evaluation home (owned by the secrets work) |
| O6 | Understanding | Intents come from regular expressions; a model is consulted only when no metric matches. A compiled purpose keeps the original verb ("decline") even when the new period rose (the answer itself is correct) | S3 | M | Neutral purpose wording; model-assisted intent with the approved-names contract for all unmatched phrasings |
| O7 | Adaptive presentation | Audience is executive or analyst by keyword only; no control to change depth or format without re-asking | S3 | M | An audience control on the answer (business, analyst, engineer, executive) that re-renders the same result |
| O8 | Efficiency | The narrative call is repeated on compiled runs even when findings are identical to a previous run | S4 | S | Cache narrative by findings hash |
| O9 | Models | No real OpenRouter or local model has been exercised end to end (no key on this machine) | S2 | S | One live run once a key is stored in Settings |
| O10 | DuckDB | One engine process owns each file (by design); no multi-process writer or read replicas | S3 | L | Documented in DUCKDB.md; revisit for multi-node |

Scenario coverage after this pass: A, C (adversarial), G (resume) and H (isolation) by existing tests; B and D by
`test_workflows.py` and the benchmark; E (cross-product continuity) partly, Build › Workflows → Analyst, checked
in the running app; F (first-time user) by the journey table above, not yet with a person.
