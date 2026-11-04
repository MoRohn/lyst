# Autonomous intelligence and operational excellence (iteration 8)

Working register for the eighth master prompt. It holds the repository assessment, what each pull request delivered
(existing limitation, root cause, solution, tests, verified result, performance impact, remaining risks), and what is
still open. Statements describe code on `main` plus the PR named. Other registers keep their scope:
`docs/PLATFORM.md` (iteration 7), `docs/ENGINEERING_EXCELLENCE.md`, `docs/PRODUCT_EXCELLENCE.md`, `docs/GAPS.md`.

Rohn's emphasis for this cycle: decision intelligence and scenario modeling; inference and resource optimization;
continuous evaluation; operational autonomy and resilience. A sibling thread owns onboarding, domain packages, the
evidence graph, data classification, approval intelligence and cost attribution; this register does not repeat them.

## Repository assessment (main at `66e4c0f`)

| Prompt section | What existed | Gap that mattered most |
|---|---|---|
| 3.1 Durable runtime | Leases with heartbeat and fencing, checkpoints per step, DAG waves, resume after crash, takeovers with a crash-loop stop, idempotency keys, step-level time budget enforced while steps run | The time budget restarted on every resume (P6-10), so a task interrupted repeatedly could run without limit |
| 3.2 Scheduling | Interval schedules (`every_seconds`), approval-gated, ticked by the worker | No calendar semantics (weekday, local time, time zone, DST); no overlap policy; downtime handled only by accident; no per-occurrence idempotency; the owner was never told a scheduled run finished |
| 3.3 Supervision | Budget and lease enforcement inside the runtime | Nothing looked at the system as a whole: stalled steps, queued work past a deadline, approvals nobody decided, schedules the worker fell behind on |
| 8 Workload | Claim order: scheduled after everything else, then oldest. Gateway admission (pools, interactive reserve, per-tenant limits) for model calls only | No task priority beyond "scheduled last", no fairness between workspaces, no admission control (queues grew without bound), no quotas, no model spend or token budget per workspace |
| 8.4 Usage | `/model-usage` per model; task trace per task | No operational picture for administrators (queues, run times, failures by class, spend against budget) |
| 2 Routing | Economy/Balanced/Frontier tiers from OpenRouter's live catalog, quick workloads at the lowest reasoning setting, fallback, retry classification, adaptive concurrency (PR #10/#16) | Routing does not use measured quality per workload; covered in a later slice |
| 5 Evaluation | `lyst eval`, 19 golden questions with independent expected values, stored scored runs | No baseline comparison, so a regression in accuracy or latency is not detected as a regression; later slice |
| 1, 6 Decisions and scenarios | Staffing scenarios with Monte Carlo inside the prescriptive analysis | No general decision model (alternatives, criteria, weights, sensitivity) or driver-based what-if engine; later slice |

## Slice 1: operations (PR #40)

Module `src/lyst/operations.py`; runtime changes in `src/lyst/runtime/__init__.py`; CONTROL migration 20.

| # | Existing limitation | Root cause | Implemented solution | Tests | Verified result |
|---|---|---|---|---|---|
| OP1 | A task paused, taken over or interrupted by a restart got a fresh time budget on every run (P6-10) | Elapsed time was measured from each run's own start and never stored | `tasks.active_seconds` is written on every lease renewal and when a run ends; a run starts its clock at the stored value; a retry of a failed task starts a fresh budget | `test_time_budget_counts_run_time_from_earlier_runs` | A task whose earlier runs used the full budget fails on resume with "time budget"; a normal run records its run time |
| OP2 | Background work competed equally with people; one busy workspace could fill every worker | The claim ordered only by "scheduled last, then oldest" | Priority by kind (analysis/workflow 0, investigation 1, scheduled 2, evaluation/maintenance 3), then deadline, then age; within a priority the workspace with the fewest running tasks goes first; background work never takes the last worker; `max_running` per workspace | `test_people_come_before_background_and_workspaces_share_workers`, `test_background_work_never_takes_the_last_worker`, `test_max_running_quota_holds_a_workspace_back` | Interactive work is claimed before older scheduled work; a second workspace is served before the first workspace's second task |
| OP3 | Queues grew without bound | No admission control | `max_queued` per workspace (default `LYST_WORKSPACE_MAX_QUEUED`=500), checked in the submit transaction; refusal is HTTP 429 with `Retry-After`; an idempotent repeat still returns the first task | `test_a_full_queue_refuses_new_work_with_429…` | Third submit refused at a limit of 2; the API answers 429 |
| OP4 | No spend or token limit per workspace | Usage was recorded, never enforced | `monthly_spend_usd` and `monthly_tokens` quotas checked in `LLMClient.chat` before any call; the refusal is an `LLMUnavailable`, so analysis continues deterministically without model wording. `task_budget_seconds` caps any one task | `test_monthly_model_budget_stops_model_calls`, `test_quota_caps_a_tasks_time_budget` | Model call refused at $5.00 of $5.00; the analysis still finishes |
| OP5 | Schedules were intervals only; no weekday, local time or DST handling | `next_run_at = now + every_seconds` | Calendar schedules: `at` (HH:MM), `weekdays`, `timezone` (IANA). Computed in the local zone with `zoneinfo`: 08:00 London is 08:00 UTC in winter and 07:00 UTC in summer; a nonexistent local time runs at the pre-jump offset; a repeated one runs once | `test_weekly_schedule_stays_at_local_time_across_daylight_saving`, `test_nonexistent_and_repeated_local_times_are_handled`, `test_bad_calendar_inputs_are_refused` | Exact UTC instants asserted across both 2026 London transitions |
| OP6 | Downtime, overlap and duplicate ticks were not handled deliberately | No record of missed runs; nothing checked the previous run; no key per occurrence | After downtime a schedule runs once and records `missed`; a run due while the previous one is active is skipped and counted (`overlap='skip'`, or `queue`); each occurrence submits with idempotency key `schedule:<id>:<occurrence>` inside the same transaction that moves `next_run_at` | `test_after_downtime_…`, `test_a_run_due_while_the_previous_one_is_still_going_is_skipped`, `test_weekly_investigation_triggers_once_runs_and_notifies` | Three missed weeks give one run and `missed=3`; a second tick in the same minute starts nothing |
| OP7 | The owner of a schedule was never told a run finished | No notification path for tasks | A scheduled task that finishes or fails raises an inbox notice (`scheduled_done` / `scheduled_failed`) for the schedule's owner and the workspace admins, deduplicated per task | `test_weekly_investigation_triggers_once_runs_and_notifies` (scenario C) | Inbox holds "Weekly performance: ready" |
| OP8 | Nothing watched the system as a whole | No supervisor | `operations.supervise` on every worker tick: stalled steps (running longer than `LYST_STALL_SECONDS`, default 300) get one event; queued tasks past `deadline_at` fail ("missed its deadline"); approvals pending over 24 h and schedules more than 10 minutes behind are listed. It never marks work done and never kills a running task (the time budget does that) | `test_supervisor_flags_stalls_expires_late_work_and_reports` | Stall recorded once across passes; the stalled task stays running |
| OP9 | Administrators had no operational picture | Per-call records were never summarized | `GET /v1/operations` (installation admins) and `GET /v1/workspaces/{ws}/operations` (workspace admins): queue by priority and status, running/queued per workspace, failures in 24 h by class, run time p50/p95 by kind over 7 days, supervisor findings, model use this month against budget. `PUT /v1/workspaces/{ws}/quotas` is installation-admin only, so a workspace admin cannot raise their own share | `test_operations_and_quota_endpoints_are_admin_only` | Viewer 403, workspace admin can read its own report but not set quotas |

Performance impact: the claim reads at most 500 candidate rows plus the live running set in its one write
transaction. Measured with `scripts/bench_claim.py` (2,000 queued tasks over 10 workspaces, four kinds, on the
development Mac): claim p50 2.8 ms, p95 3.3 ms, max 3.5 ms. The budget check adds one indexed aggregate over `llm_calls` per model call, only when a
workspace has a model budget.

Remaining risks: quotas and fairness are per engine process (Lyst runs one engine per installation, see decision
0001); a step thread that overruns still cannot be killed (P6-9); `deadline_at` is accepted by `submit` but no API or
interface sets it yet; the supervisor's stall threshold is one global setting.

Interface: none in this PR. The operations report and quotas belong under Settings (installation and workspace
sections), placement to be agreed with the UX thread, which owns `src/lyst/ui/`.

## Slice 2: decision intelligence and scenario analysis

Modules `src/lyst/scenarios.py` and `src/lyst/decisions.py`; a `what_if` intent and capability in the analyst;
WORKSPACE migration 20 (`scenario_models`, `scenarios`, `decisions`); permissions `scenario.define` and
`decision.create` (editor). The demo workspace gets a driver model, `pnl_drivers` (revenue, COGS ratio and operating
expenses observed for the last quarter; labor share of operating expenses an assumption with a range, since the demo
P&L has no labor line).

| # | Existing limitation | Root cause | Implemented solution | Tests | Verified result |
|---|---|---|---|---|---|
| DI1 | "What happens to operating margin if labor costs increase 6% while revenue grows 3%?" was read as an explain question about the past | No what-if model or intent | Driver models: inputs (assumed, or observed through the SQL guard from a governed metric or a dataset measure for a named period), arithmetic formulas parsed with `ast` (no `eval`), outputs. A what-if sentence is parsed into changes on any node; unknown drivers are listed, never guessed. The Home question plans `what_if → validate → deliver`; the answer is labelled hypothetical and lists observed inputs (period, dataset version, data-through), assumptions and formulas; each observed input's query is evidence | `test_what_if_sentences_are_parsed…`, `test_scenario_e_what_if_from_home…` | Margin and base equal an independent pandas recomputation from raw rows (rel. 1e-9) |
| DI2 | No sensitivity, uncertainty or best/worst analysis outside the staffing forecast | — | Tornado sensitivity (range ends or ±10%) with elasticity; best/base/worst from each input's favourable end; seeded vectorized Monte Carlo (normal, triangular, uniform; relative ranges for observed inputs) with P5/P50/P95, histogram and probability below a threshold | `test_sensitivity_cases_and_simulation…` | Worst < P5 < P50 < P95 < best; same seed gives identical output |
| DI3 | Changing one assumption would recompute everything | — | Evaluation with a previous result recomputes only nodes downstream of the changed overrides; observed inputs are cached per dataset version | `test_only_downstream_nodes_are_recomputed`, `test_scenario_e_adjust_save_reproduce_and_compare` | Changing labor share recomputes 5 of 9 nodes; incremental equals full recalculation |
| DI4 | No saved, versioned, reproducible scenarios | — | Named scenarios saved as versions with their result and a digest of model version, changes and observed inputs; `reproduce` re-runs on the pinned model version and reports any difference and which observed inputs moved; compare saved scenarios of one model | same | Reproduce returns `same` with no moved inputs |
| DI5 | Validity of what-if inputs unchecked | — | Validation: input ranges, changes over 50%, non-finite results (division by zero said plainly, not an exception), observed data older than 120 days | `test_scenario_validation_flags…` | Revenue −100% gives a `fail` on margin; viewers may explore but not define models |
| DI6 | No structured decision support (scenario A) | — | `frame` (objective, alternatives, constraints, analysis vs decision needing authorization, what is missing); `analyze` (min-max weighted criteria, constraints with reasons, triangular uncertainty → probability each ranks first and score intervals, weight at which the leader changes, cost-benefit and risk-adjusted score); `allocate` (SciPy `milp`/HiGHS with groups and unit bounds) | `test_framing…`, `test_scenario_a_compares…`, `test_allocation_under_a_budget_is_optimal` | Infeasible option removed with its reason; allocation equals brute force |
| DI7 | Recommendations could be stated with unsupported certainty | — | Recommendation contract: evidence, assumptions, expected impact, uncertainty, alternatives, risks, implementation, monitoring; `insufficient_evidence` without evidence, `no_clear_winner` when the top two are not separated beyond the uncertainty; always `requires_human_decision` | `test_no_false_certainty…` | Identical alternatives give no winner |
| DI8 | No reviewable decision workflow | — | Decision records: analyzed → submitted (approval request `decision.approve`; the person may choose differently from the recommendation, both kept) → approved/rejected by an approver → reviewed against the expected impact (met / partly met / not met). Nothing is executed | `test_decision_record_needs_a_person…` | Approval by another member moves it to approved with a review date |

API: `/v1/workspaces/{ws}/scenario-models[/{name}[/evaluate|/analyze|/scenarios]]`, `/scenarios/compare`,
`/scenarios/{id}/reproduce`, `/what-if`, `/decision-records[/frame|/allocate|/{id}[/submit|/review]]` (decision records; `/decisions` is the collaboration list of comments recorded as decisions).

Performance: a what-if on the demo model runs three aggregate queries once per dataset version (cached afterwards);
`scripts/bench_scenarios.py` on the development Mac: first what-if 56 ms (reads the data), repeated 6.5 ms (cached
base), one assumption changed 4.1 ms, sensitivity + best/worst + 10,000-draw simulation 5 ms (median).

Remaining risks: formulas are arithmetic only (no conditionals or time series: a scenario is one period); the
what-if parser understands percentage changes ("rises 3%", "falls by 2 percent"), not absolute amounts in a sentence
(the API takes `value` and `change_abs`); OR-Tools and Pyomo were not added because SciPy's `milp` covers the
allocation problems here; the scenario explorer and decision views are not built yet (placement agreed with the UX
thread first).

### Fix after slice 2 (reported from the website and README threads)

| # | Defect | Root cause | Fix | Test |
|---|---|---|---|---|
| DI9 | "What if revenue falls 5% and the COGS ratio rises 2 points next quarter?" applied only the revenue change, said nothing about the COGS ratio, and the answer was marked verified | The parser read percentages only; a change it did not recognise was skipped silently, and the verdict ignored it | "Points" changes apply to ratios and percentages (2 points = +0.02); every stated change with an amount that is not applied is listed ("I couldn't apply: … , so these figures leave it out") and the answer is `unverified`, never Verified | `test_points_changes_apply_and_unapplied_changes_are_stated_never_dropped` |
| DI10 | A what-if answer crashed the Home renderer (`KeyError: 'rows'`) | What-if evidence entries lacked `rows` and `ms` | Evidence entries carry the same keys as every other query (`purpose, sql, query_id, rows, ms, datasets`) | same |
## Slice 3: continuous evaluation and measured routing

Changes in `src/lyst/evals.py`, `src/lyst/cli.py`, `src/lyst/llm/scorecard.py`, `src/lyst/llm/__init__.py`; checked-in
baseline `docs/quality/eval-baseline.json`; CI runs `lyst eval --baseline docs/quality/eval-baseline.json`.

| # | Existing limitation | Root cause | Implemented solution | Tests | Verified result |
|---|---|---|---|---|---|
| EV1 | A change that made answers slower, costlier or wrong on one case failed CI only if a whole case failed outright; resource growth and latency drift were invisible | Runs were stored but never compared with an earlier run | Baseline per case: which checks passed, seconds, steps, queries, model calls, tokens. `evals.compare` reports accuracy and completion regressions (a check that passed now fails), latency regressions (above max(baseline × 3, baseline + 1 s), so machine noise is not one), resource regressions (above baseline × 1.5 and + 2), and missing cases. `lyst eval --baseline` fails on any regression; `--write-baseline` records a reviewed run | `test_compare_flags_latency_resources_and_missing_cases_but_not_noise`, `test_checked_in_baseline_covers_the_whole_bank` | 0.9 s against a 0.2 s baseline is noise; 2.5 s is a latency regression |
| EV2 | Scenario H: a controlled analytical defect had to be caught before release | — | The regression test plants a defect in metric SQL (every value scaled by 0.97) and runs the bank against the baseline taken before it | `test_scenario_h_a_planted_analytical_defect_is_caught_as_a_regression` | Caught as an accuracy regression on `revenue-last-quarter / value` |
| EV3 | Scores were pass/fail per case only | — | Quality dimensions per run (completion, correctness, evidence, efficiency) and pass counts per category (financial, statistical, forecasting, data quality, adversarial, scenario); resources per case | `test_runs_report_quality_dimensions…` | `lyst eval` prints both lines |
| EV4 | The bank had no scenario or what-if cases | — | Two cases: a what-if with an expected margin computed from raw rows, and an adversarial what-if naming a driver the model lacks (must say "Not modelled", never guess). 21/21 | baseline file | 21/21 on the development Mac, p95 0.22 s |
| RT1 | Routing ignored how models actually performed here (ENGINEERING M-R1) | Tier order was fixed; outcomes were recorded but not read back | Scorecard per model and workload from `llm_calls` (success rate, p50/p95 latency, tokens, cost over 7 days). In automatic routing a model whose calls for this workload mostly failed recently (≥10 decided calls in 24 h, success < 70%; all three settable) moves behind the healthy models of its tier; it stays as a fallback, is never promoted, and a pinned model is never reordered | `test_scorecard_and_measured_order`, `test_routing_skips_a_failing_model_first…` | Against a stub OpenRouter, the failing first choice is not called first; pinned, it is |
| RT2 | No single view of what each model offers and how it measured up | Catalog and usage were separate endpoints | `GET /v1/workspaces/{ws}/model-registry` (audit readers): per model, tiers, context, prices per million tokens, reasoning settings, local or not, and measured rows per workload; the workspace's inference policy | `test_model_registry_shows…` | A model used here but no longer in the catalog is still listed with its measurements |

Remaining risks: measured routing reacts to failures and errors, not to answer quality; quality per model comes from
`lyst eval` runs with a model configured, which CI does not have (no key), so the checked-in baseline is the
rules-only path. Latency baselines come from the development Mac; the factor and floor absorb CI's slower runners.

## Slice 4: skill discovery and conversational drill-down

Modules `src/lyst/skills/discovery.py` and `src/lyst/followups.py`; small hooks in the analyst (`understand`), the
runtime (`submit(derived_from=…)`) and the API.

| # | Existing limitation | Root cause | Implemented solution | Tests | Verified result |
|---|---|---|---|---|---|
| SK1 | A request worded differently from a skill's triggers drafted a second skill for a method Lyst already had | `SkillService.match` compares trigger phrases only | Discovery: BM25 over purpose, triggers, name and parameters with light stemming and a business-synonym table, over approved skills and built-in capabilities; each skill checked for compatibility (input datasets present, columns its SQL names still exist), permission, last test report and use count. When the analyst would draft a new capability, it first reuses a compatible, approved skill that discovery ranks first | `test_scenario_d_a_paraphrased_request_reuses_the_validated_skill` | "Calculate the market concentration of revenue across regions last quarter" runs `hhi_concentration`; the HHI equals pandas; no new skill version is drafted |
| SK2 | Nothing explained why a skill was not used | — | Unusable candidates are listed with reasons (missing column, permission, failed tests); discovery never changes a skill | `test_an_incompatible_or_failing_skill…` | A renamed input column is named in the reason; nothing is reused |
| DD1 | A follow-up had to be retyped as a full question, and the earlier metric, period and filters were re-guessed | No link between tasks | `POST /v1/tasks/{id}/follow-up`: the follow-up is resolved against the parent task's verified understanding (metric, period, member filters, split): "break that down by region", "compare it to last year", "top contributing…", "what about Q1 2026", "make this a dashboard", "schedule this every Monday at 9" (an approval-gated calendar schedule) | `test_follow_up_phrases…`, `test_drill_down_carries_context…`, `test_follow_up_endpoint…` | "Break that down by product line" after "revenue in EMEA last quarter" keeps EMEA and Q3 2026; Hardware equals pandas |
| DD2 | Hidden session assumptions could change results | — | Each follow-up task stores `derived_from` (parent task, the follow-up words, what changed, what was carried) and an explicit question; re-asking the recorded question reproduces the tables. Follow-ups Lyst cannot honour (dropping "unusual" rows, a significance test) say so and offer what it can do, never silently changing the number | same | Re-run tables identical |

Remaining risks: discovery is lexical with synonyms (no embeddings yet; the knowledge layer's embedding model could
be added when configured); drill-down covers the follow-ups listed above, and anything else asks for a full question.

## Slice 5: long-term memory that ages, and operational continuity

Changes in `src/lyst/memory.py`, `src/lyst/continuity.py`, `src/lyst/store/__init__.py` (memory retention), the analyst
(`learn` records what a memory was learned from; answers flag memory that may no longer apply) and the API.

| # | Existing limitation | Root cause | Implemented solution | Tests | Verified result |
|---|---|---|---|---|---|
| ME1 | A remembered answer stayed "true" after the data or the metric definition changed | Memory kept its text and trust only | Episodic memory records its sources (dataset, version) and metric version; `applicability` marks a memory stale when a source has a newer version or no longer exists, the definition changed, or (unless approved) it is older than `LYST_MEMORY_STALE_DAYS` (365). Recall ranks memories that still apply first and returns the reason; answers that draw on stale validated memory say "may no longer apply: …" | `test_a_memory_learned_from_older_data…`, `test_a_changed_metric_definition_or_age…` | New pnl version: "it was learned from pnl v1; the data is now v2" |
| ME2 | No way to review knowledge that went stale | — | `GET /v1/workspaces/{ws}/memory/review` (admins): every memory that may no longer apply, with the reason, to revoke or re-confirm | `test_memory_review_endpoint_is_for_admins` | Viewer 403 |
| ME3 | Memory was kept forever | No retention | `LYST_RETENTION_DAYS_MEMORY` in the daily prune removes old agent-observed episodic and task memory (and its vectors); approved and person-validated knowledge is never aged out | `test_memory_retention_removes_only_old_agent_observations` | Old observation removed, validated kept |
| DR1 | A backup interrupted part way (full disk) left a half-written folder that looked like a backup | Files were written in place | Backups are written to `.partial-<stamp>` and renamed when complete; a failure removes the partial copy, keeps earlier backups and records the error | `test_backup_is_atomic…` | ENOSPC mid-backup: no partial folder, the previous backup intact |
| DR2 | Nothing checked that a backup could actually be restored until someone tried | Restore verified only at restore time | Restore drill: checksums, key separation, copy, open every database with the current master key, read schema version and tables, timed | `test_restore_drill_verifies…` | Flipped byte: drill fails on the checksum |
| DR3 | No scheduled backups, so the recovery point was whenever someone remembered | — | `LYST_BACKUP_EVERY_HOURS` on the worker tick: backup, drill, keep the newest `LYST_BACKUP_KEEP` (7); partial leftovers cleaned | `test_scheduled_backups_respect_the_interval_and_retention` | Interval respected; oldest pruned |
| DR4 | Recovery objectives were not measured | — | `GET /v1/installation/continuity`: backups, last failure, last drill, RPO (age of the newest drilled backup) and RTO (that drill's time) against `LYST_RPO_HOURS` / `LYST_RTO_MINUTES`; `POST /v1/installation/backups/{stamp}/drill`. No high-availability claim (one engine per installation) | `test_continuity_endpoints…` | Demo installation (9.7 MB): backup 0.03 s, drill 0.05 s on the development Mac |
| DR5 | Key separation was a note in the manifest | — | Backups are checked to contain neither the master key bytes nor a key file; the drill refuses a backup with key material; the report says the key comes from its trust anchor, backed up separately | `test_backups_never_contain_the_master_key` | Key bytes absent from every backup file |
| DR6 | A migration failing part way could leave a half-applied schema | (checked) | Already one transaction per run: verified by a test that a failing second migration leaves version 1 and no new table | `test_a_failed_migration_leaves_the_database_as_it_was` | Rolled back |

Recovery cases already covered elsewhere: worker crash and engine restart (`test_runtime_integrity.py`, scenario G in
`test_operations.py`), model service failure and expired credentials (`test_gateway.py`, `test_provider_key_failures.py`),
corrupted backup and restore over a running engine (`test_backup_integrity.py`). Not simulated here: network outage of
external connectors beyond the connector retry tests, and an interrupted deployment (needs a deployment target).
