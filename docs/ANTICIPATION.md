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

Status: `candidate -> prepared -> surfaced -> accepted | dismissed | snoozed`, with `expired` past validity, `withdrawn`
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

## API

| Route | Purpose |
| --- | --- |
| `GET /v1/workspaces/{ws}/anticipation/settings` | Level, budgets, today's usage, whether external services are used |
| `PATCH /v1/workspaces/{ws}/anticipation/settings` | `{level, daily_prepare_limit, daily_investigation_limit, daily_compute_ms, daily_surface_limit}` (admins) |
| `GET /v1/workspaces/{ws}/anticipations?status=open&kind=` | The activity center for the caller |
| `POST /v1/workspaces/{ws}/anticipations/refresh` | Run a cycle now (admins) |
| `GET /v1/workspaces/{ws}/anticipations/{id}` | One item with its checks, dependencies, resources and event trail |
| `GET /v1/workspaces/{ws}/anticipations/{id}/why` | The grounded explanation: `{short, detail[], why[], signals, checks, score, scores, depends_on, level_required, methods}` (`short`/`detail` match the evidence explain shape) |
| `POST /v1/workspaces/{ws}/anticipations/{id}/act` | `{action: run_now | dismiss | snooze | useful | not_useful | incorrect, until?, note?}` |

## Storage

WORKSPACE migration 50: `anticipations`, `anticipation_events`, `anticipation_settings`, `anticipation_usage`,
`attention_prefs` (used by the attention slice). This lane owns migrations 50-59.

## Measurements

`test_e_prepare_warns_and_automate_refreshes_the_input_before_the_run`: preparing a connection-fed weekly run at level 4
performs one incremental sync before the run (one new row picked up, a new dataset version), with no model calls, well
inside a 5 s bound; at level 2 the same item only warns and writes nothing.

## Gaps (prioritized)

| # | Gap | State |
| --- | --- | --- |
| N1 | Next-best analytical action after completed work (scenario D) | Planned, slice 2 |
| N2 | Early-warning analytics and read-only investigation before notifying (scenario B) | Planned, slice 3 |
| N3 | Data and integration health foresight: schema-change impact with validated repair proposals, credential age and expiry, latency and failure trends (scenarios C, F) | Planned, slice 4 |
| N4 | Attention management: per-person frequency, quiet periods, relevance thresholds, muted kinds, opt-out (scenario H) | Planned, slice 5 (table exists) |
| N5 | Learning from outcomes and anticipation quality metrics; offline verification (scenario I) | Planned, slice 6 |
| N6 | Capability-gap forecasting through the Capability Foundry (scenario G) | Planned, slice 7 |
| N7 | Consume the unified event model (CONTROL 40) instead of after-ingest hooks once it is merged | Planned |
| N8 | Use the operations calendar (`schedule_next`) for later occurrences and submit preparation tasks at maintenance priority once operations is merged | Planned |
| N9 | Activity center, inline suggestions and Settings controls in the UI (UX thread) | Handed over |
