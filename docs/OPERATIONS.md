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

## Slice 1: operations (this PR)

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
