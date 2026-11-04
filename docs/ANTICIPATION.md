# Anticipative intelligence

Lyst notices what is likely to be needed next, prepares it inside explicit bounds, says why, and stays quiet when there
is nothing worth a person's attention. This register records what is built and tested, how it works, and what is still
open. It is kept honest: "Planned" means not built.

## Architecture

`src/lyst/anticipate/` is one engine over existing Lyst services, not a separate product:

| Module | Role |
| --- | --- |
| `policy.py` | Autonomy levels 0-4, the operation each level allows, per-workspace daily budgets and usage counters |
| `model.py` | Prepared work as explicit objects (`anticipations`) with an append-only trail (`anticipation_events`) |
| `service.py` | The cycle, the activity center, explanations, a person's responses, level changes |
| `recurring.py` | Anticipator: approved recurring work (schedules) prepared just in time |
| `nextaction.py` | Anticipator: next-best analytical action after finished work |
| `health.py` | Anticipator: credential expiry and age, connection reliability trends, schema-change impact with a validated fix |
| `attention.py` | Per-person attention preferences applied to pushes and to the activity center |
| `earlywarning.py` | Anticipator: developing adverse trends on monitored metrics, investigated before anyone is told |

The cycle per workspace: **observe** (signals from authorized metadata) -> **contextualize** (the run's datasets and
definitions, from its compiled workflow, the metrics its question names and the datasets its last run queried) ->
**forecast** (the lead window from recent run durations) -> **anticipate** (a candidate per occurrence) ->
**prioritize** (score) -> **prepare** and **verify** (read-only checks with a verdict) -> **surface** (only blockers are
pushed; ready work waits in the activity center) -> **learn** (feedback recorded; ranking from outcomes is the next
slice).

It runs on the worker tick at most every `LYST_ANTICIPATE_EVERY` seconds (default 300), and right after a dataset
version or a definition changes so prepared work is re-checked at once. Everything is deterministic and local: no model
call, no network request, except a level-4 refresh of an already-configured connection.

### Autonomy levels

| Level | Name | What may run before anyone asks |
| --- | --- | --- |
| 0 | off | Nothing. Open items are withdrawn and queued background tasks they started are cancelled |
| 1 | suggest | Metadata only: items are listed; no checks run (the default) |
| 2 | prepare | Bounded read-only checks |
| 3 | investigate | Read-only background investigations (next slices) |
| 4 | automate | An approved, already-configured connection sync may refresh a stale input before an approved run |

Levels never widen permissions: every check and action runs as a real member through the guard, and consequential
actions (sending, publishing, editing data, definitions or deliverables, deploying a capability) go through approvals at
every level. Only workspace admins change the level and budgets; the change is audited.

### Prepared-work object

Objective, subject, audience, signals, reasons (plain words, each from a signal, check or dependency), score and its
components (value, likelihood, evidence, urgency, cost, risk, interruption), level required, status, verdict, prepared
inputs, verification checks, offered actions, dependencies (dataset and metric versions, workflow version and status, the
owner's role, the occurrence), resources used (compute ms, queries, model calls, refreshes), needed-at, valid-until,
revision, inbox finding, task.

Status: `candidate -> (preparing, while a background investigation runs) -> prepared -> surfaced -> accepted | dismissed | snoozed`, with `expired` past validity, `withdrawn`
when the level drops, and any open item re-opened as a candidate (revision + 1, event `invalidated` naming what changed)
when a dependency changes.

### Scoring

`score = 100 * value * likelihood * (0.5 + 0.5 * evidence) * (0.4 + 0.6 * urgency) * learned - 10 * cost - 15 * risk -
5 * interruption`, with urgency `1 / (1 + hours_until_needed / 6)`. Components are stored with the item and shown in the
explanation. `learned` is 1.0 until outcome-based ranking lands. No component is a model-generated confidence.

## Recurring work (scenario A, E)

Signal: an enabled, approved schedule whose next run is inside its lead window (three times the slowest recent run,
between one hour and a day). Checks as the schedule's owner: access, each input's availability and freshness since the
last run, a failing connection behind an input, a broken data contract, each metric still approved and whether its
definition changed since the last run, a stale compiled workflow (the run would fail), recent failures, the run-time
estimate, and the workspace's model budget when the operations module is present. Verdict: `ready`,
`ready_with_warnings` or `blocked`. A blocked item is raised once to the inbox through `notices.raise_notice`, within the
daily surface budget. The only offered action is "Run it now", which submits the same request as the person accepting.

## Next-best analytical action (scenario D)

Signal: an analysis, investigation or follow-up finished in the last 7 days. Each suggestion is grounded in what that
analysis found and checked before it is shown; at most three per analysis, never for a question the person already asked
(compared by question pattern, period aside), never pushed to the inbox:

| Suggestion | When | Verified by | Action |
| --- | --- | --- | --- |
| Look inside the driver | one member explains at least half of the change and the metric has a dimension the analysis did not use | counting the rows of that slice as the person (an empty slice or lost access drops it) | run the drill-down question now, as the person (`kind=followup`) |
| Watch the metric | a change large enough to attribute drivers, and no monitor on the metric | the metric is still approved | request a monthly monitor; an admin still approves it |
| Make it recurring | the same question asked 3+ times in 60 days, no schedule | - | propose a schedule at the observed rhythm (day, week or month); an admin still approves it |
| Extend into a forecast | a quarter or year reviewed, no forecast of it in 30 days | at least 12 months of history | run the forecast question now |

`GET /v1/workspaces/{ws}/anticipations?subject=<task id>` lists the suggestions for one answer.

## Early warning (scenario B)

Scope: metrics with an approved, enabled monitor, read as the monitor's creator through the monitor's own read path
(complete periods only, column and row policies apply). Detection needs level 2; investigation needs level 3.

Signals on at least 12 periods: a **change point** (a new level of at least three periods that began in the last five, at
least 2 sigma the adverse way), a **sustained trend** (four adverse steps totalling at least 1.5 sigma), **forecast
drift** (refit without the last three periods: all three on the adverse side, two outside the 80% interval) and a
**projected breach** (the next two forecast periods cross the monitor's alert line). Sigma is the standard deviation of
the history before the recent window. A change point and forecast drift are two views of one level shift, so evidence
is counted in three kinds (level, trend, projection); a warning needs two kinds or a change point of at least 4 sigma.

Suppressed silently: stale data, a move that repeats the same periods last year (seasonal), one kind of evidence only,
a period the monitor already alerted on, and (level 3) an investigation that fails or cannot attribute the move with
verified confidence. At level 3 the warning becomes a read-only investigation task (kind `investigation`, maintenance
priority 3, `deadline_at` before the next period) as the monitor's creator; once it completes with verified confidence and
drivers, the finding (answer, drivers, implications, next steps, the forecast's interval and back-test error) is pushed
once to the inbox. At level 2, or when the day's investigation budget is used, the warning waits in the activity center
marked `unverified`.

Lowering the level below 3 withdraws warnings whose investigation has not finished and cancels the queued task.

## Data and integration health foresight (scenarios C, F)

From metadata Lyst already records; no source is contacted. Level 1 lists these items; level 2 pushes the risky ones once.

| Signal | Source | Pushed when | Offered |
| --- | --- | --- | --- |
| Credential expiry | `expires_at` recorded with the credential behind a connection | expired, within 3 days, or before the next scheduled run that reads the connection's data | link to Settings (`credential_id`, `connection_id` in the action; the secret is never shown) |
| Credential age | connected or last replaced more than `LYST_CREDENTIAL_MAX_AGE_DAYS` (180) ago | never (activity center) | same link |
| Authentication failure | the latest connection event failed with category `auth` | always | test the connection, open the source |
| Intermittent failures | 2+ of the last 10 attempts failed, the latest succeeded | a dependent run within 48 h | same |
| Rate-limit pressure | a third of recent attempts retried for rate limits | a dependent run within 48 h | same |
| Rising latency | median of the last 5 successful calls at least twice the earlier median and over 1 s | a dependent run within 48 h | same |
| Schema change | a dataset version in the last 14 days removed or retyped columns approved metrics use | any schedule or asset depends on the metrics | a validated definition fix filed as a review |

Schema-change repair: for each removed column a replacement of the same type family with a similar name is chosen; the
corrected definition is compiled and run on the new version as the metric's owner, and only a candidate that runs is
proposed. Accepting files one `metric.define` review per metric (collaboration reviews), so an admin approves each
definition change; the asset registry then shows the dependent reports and dashboards as stale with its one-click
repair. Items close themselves (`resolved`) when the signal is gone, for example after the credential is replaced with a
later expiry.

## Attention management (scenario H)

Per person, per workspace (`attention_prefs`): opt out, minimum score, daily cap on pushed items (default 3), quiet hours
(HH:MM to HH:MM in an IANA time zone, may wrap midnight) and muted kinds. Each cycle gathers every item worth pushing
and takes them highest score first. For each person who would be told (the item's audience, plus workspace owners and
admins as for any notice):

* opted out, muted kind, under their minimum score, or the same subject pushed to them in the last 24 hours: not told;
* in quiet hours or at their daily cap: the item is **held** for its audience and retried next cycle (logged once).

If nobody may be told and nothing is held, the item stays in the activity center only (`not_pushed`). The activity center
leaves out muted kinds and items under the person's minimum score, and is empty for someone who opted out. The
workspace's `daily_surface_limit` still caps all pushes. `notices.raise_notice` gained an `exclude` set for this.

## API

| Route | Purpose |
| --- | --- |
| `GET /v1/workspaces/{ws}/anticipation/settings` | Level, budgets, today's usage, whether external services are used |
| `PATCH /v1/workspaces/{ws}/anticipation/settings` | `{level, daily_prepare_limit, daily_investigation_limit, daily_compute_ms, daily_surface_limit}` (admins) |
| `GET /v1/workspaces/{ws}/anticipations?status=open&kind=` | The activity center for the caller |
| `POST /v1/workspaces/{ws}/anticipations/refresh` | Run a cycle now (admins) |
| `GET /v1/workspaces/{ws}/anticipations/{id}` | One item with its checks, dependencies, resources and event trail |
| `GET /v1/workspaces/{ws}/anticipations/{id}/why` | The grounded explanation: `{short, detail[], why[], signals, checks, score, scores, depends_on, level_required, methods}` (`short`/`detail` match the evidence explain shape) |
| `GET /v1/workspaces/{ws}/attention` | The caller's attention preferences and the kinds they can mute |
| `PUT /v1/workspaces/{ws}/attention` | `{opted_out, min_score, max_per_day, quiet_start, quiet_end, timezone, muted_kinds}` |
| `POST /v1/workspaces/{ws}/anticipations/{id}/act` | `{action: run_now | dismiss | snooze | useful | not_useful | incorrect, until?, note?}` |

## Storage

WORKSPACE migration 50: `anticipations`, `anticipation_events`, `anticipation_settings`, `anticipation_usage`,
`attention_prefs` (used by the attention slice). This lane owns migrations 50-59.

## Measurements

`test_e_prepare_warns_and_automate_refreshes_the_input_before_the_run`: preparing a connection-fed weekly run at level 4
performs one incremental sync before the run (one new row picked up, a new dataset version), with no model calls, well
inside a 5 s bound; at level 2 the same item only warns and writes nothing.

Early-warning calibration (`uv run python scripts/bench_earlywarning.py 200`; synthetic monthly series, 30 months,
Gaussian noise; "replayed" walks a six-month decline month by month and compares the first month early warning raises
with the first month the monitor's own rule, robust z >= 3 on the latest month, alerts):

```
runs per case: 200
false positives, noise:            0.0%
false positives, noise + blip:     2.5%
decline 0.5 sigma/month: detected after 4 months 9.0%; replayed: earlier than the monitor 29%, same month 27%, later 44%, never 60%, mean lead -0.2 months
decline 1.0 sigma/month: detected after 4 months 53.0%; replayed: earlier than the monitor 21%, same month 29%, later 50%, never 6%, mean lead -0.6 months
decline 1.5 sigma/month: detected after 4 months 89.0%; replayed: earlier than the monitor 11%, same month 27%, later 62%, never 0%, mean lead -0.8 months
decline 2.5 sigma/month: detected after 4 months 100.0%; replayed: earlier than the monitor 2%, same month 21%, later 76%, never 0%, mean lead -1.1 months
```

Reading: the detector is conservative (2.5% false positives on noise with a final 2.5-sigma blip, before the
investigation stage filters further). Against a 3-sigma monitor it is rarely earlier: for fast declines the monitor's
single-period rule fires first. Its value today is that a warning arrives investigated, with drivers and uncertainty,
and that slow drifts (0.5-1 sigma a month) are caught earlier in about a quarter to a third of the cases where the
monitor would catch them at all. Earlier detection of fast moves without more false positives is open (N10).

## Gaps (prioritized)

| # | Gap | State |
| --- | --- | --- |
| N1 | Next-best action: comparisons against other periods, hypothesis tests and briefings as further suggestion kinds | Planned |
| N2 | Early warning on unmonitored metrics, leading-indicator search across metrics (correlation without causal claims), capacity-exhaustion and cost-variance projections | Planned |
| N3 | Health foresight: volume anomalies and incomplete ingestion, storage pressure, API deprecation notices, retyped-column repair candidates, predicting breakage before a connection sync commits the new version | Planned |
| N4 | Attention: digest delivery for held items, per-person budgets on background compute | Planned |
| N5 | Learning from outcomes and anticipation quality metrics; offline verification (scenario I) | Planned, slice 6 |
| N6 | Capability-gap forecasting through the Capability Foundry (scenario G) | Planned, slice 7 |
| N7 | Consume the unified event model (CONTROL 40) instead of after-ingest hooks once it is merged | Planned |
| N8 | Use the operations calendar (`schedule_next`) for later occurrences; submit investigation tasks at maintenance priority with a deadline (operations is merged; the model-budget check already uses it) | Planned |
| N10 | Early warning that leads a 3-sigma monitor on fast declines without raising false positives (sequential tests such as CUSUM calibrated per metric) | Planned |
| N9 | Activity center, inline suggestions and Settings controls in the UI (UX thread) | Handed over |
