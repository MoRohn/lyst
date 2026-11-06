# Events: the event engine

Lyst has one event stream per workspace. Everything that reacts to change (webhooks, the SDK, the UI, anticipation,
and the rules and activities that later phases add) reads it. This page describes phase 1 of the event-driven
autonomy work: the canonical envelope, durable emission, durable processing and delivery to clients.

Code: `src/lyst/events.py` (the service), `src/lyst/api/events.py` (stream, detail, integration events, dead letters),
`src/lyst/api/extend.py` (the long-poll list, unchanged). Schema: control migration 110, workspace migration 110.
The events lane owns migration range **110-119**.

## Where events come from

| Origin | How | Examples |
| --- | --- | --- |
| Audit log | Derived: every audited action in `events.CATALOG` becomes an event | `dataset.version_created`, `metric.defined`, `approval.decided` |
| Tasks table | Derived: done, failed, awaiting approval | `task.completed`, `task.failed` |
| Services | **Staged** in the same workspace transaction as the state change (transactional outbox) | `dataset.schema_changed`, `asset.health_changed` |
| Integrations | Posted by a workspace admin: `POST /v1/workspaces/{ws}/events`, type `external.<system>.<what>` | `external.crm.deal_closed` |
| The engine | Records about itself | `event.chain_stopped`, `event.dead_lettered` |

Derivation means no service is instrumented twice: a new audited action becomes an event by adding one line to
`CATALOG`. Staging is for facts the audit log cannot express, such as which columns a new dataset version added.

### The transactional outbox

`EventService.stage(c, ws, type, ...)` writes to the workspace's `event_outbox` on the caller's open transaction, so
the event exists exactly when the change it describes commits (and never when it rolls back). The collector moves
outbox rows into `platform_events` in the control database and stamps `published_at`. The two databases are separate
DuckDB files, so the move is not one transaction. If the process dies between the two writes, the row is published
again on the next sweep and the event id (`o:<ws>:<outbox id>`) keeps it from being stored twice. Emitting nudges the
collector at once; every outbox is also swept every 30 seconds as crash recovery.

## The envelope (version 2)

Typed as `events.Event` (pydantic); JSON Schema in `docs/events/envelope.schema.json`, regenerated with
`uv run python scripts/event_schema.py` and checked by `tests/test_event_engine.py`.

| Field | Meaning |
| --- | --- |
| `seq` | The stream cursor. Strictly increasing; a client applies an event only when its `seq` is above the last one it applied, so a stale event never overwrites newer state. |
| `id` | Stable, unique (`a:<audit id>`, `t:<task>:<status>`, `o:<ws>:<outbox id>`). |
| `type`, `type_version`, `schema` | The contract: event type, its version, and the envelope version (2). |
| `workspace_id`, `occurred_at`, `actor`, `source` | Scope, time, who (a user id, `system`), and origin (`audit`, `task`, `service`, `api`, `rule`, `schedule`, `system`). |
| `subject`, `related` | The object the event is about, and other objects it concerns (an asset's datasets and metrics). |
| `correlation_id`, `causation_id`, `depth` | Ancestry. Everything one root event led to shares its correlation id; the causation id is the event that directly caused this one; depth counts hops from the root. |
| `sensitivity` | `public`, `internal`, `confidential`, `restricted`; follows the subject dataset's classification. |
| `dedupe_key` | The same fact emitted again with the same key is one event. |
| `data` | Ids, versions, counts and outcomes only: never an answer's text or a row of data. |

Version 1 consumers keep working: version 2 only added fields.

## Processing: at least once, idempotent

A **durable consumer** is a named handler registered with `events.consume(name, handler, types=[...])`. It has a
stored cursor (`event_consumers`) and an inbox (`event_inbox`) recording how each event went: `done`, `retry` (with
the next attempt time) or `dead`. The worker tick calls `events.dispatch()`.

* Delivery is **at least once**, never assumed exactly once. A handler that returns is recorded `done` for that event;
  a redelivery (a replayed cursor, a crash after the handler ran but before `done` was written) is skipped when the
  inbox already says `done`, and otherwise runs again, so handlers must be idempotent (use the event id or a dedupe
  key as the idempotency key of whatever they create, as `runtime.submit(idempotency_key=...)` allows).
* Handled events are recorded `done`, and the cursor moved past them, once per 50 events (and always before a
  failure is recorded), so a crash re-delivers at most that batch.
* A failing handler is retried after 5 s, 30 s, 2 min, 10 min, then every 30 min, up to its attempt cap (default 5).
  Raising `PermanentFailure` dead-letters at once. A dead event (poison) stops only itself: the cursor has already
  moved on and later events are processed. Each dead letter also writes an `event.dead_lettered` event.
* Retries are not ordered against newer events for the same subject. Handlers that maintain state must compare
  versions or `seq`, not assume arrival order.
* Admins list dead letters (`GET /v1/workspaces/{ws}/event-dead-letters`) and put one back after a fix
  (`POST .../event-dead-letters/{consumer}/{seq}/retry`).
* A new consumer starts at the current end of the stream; it does not replay history.

The in-memory `subscribers` list stays for cheap nudges (anticipation marks a workspace for an early cycle). It is best
effort and must not be used for work that has to happen.

## Chains and loop protection

An event emitted because of another (`stage(..., cause=event)`) inherits the cause's correlation id and is one hop
deeper. A task started because of an event records `{"trigger": {id, correlation_id, depth}}` in its request, and its
`task.*` events continue that chain. Past `LYST_EVENT_MAX_DEPTH` hops (default 6) the event is refused and one
`event.chain_stopped` record is written for that chain and type, so rules or handlers that trigger each other stop
instead of running for ever (`test_rules_that_trigger_each_other_stop_at_the_chain_limit`).

## Delivery to clients

* **Long poll** (unchanged): `GET /v1/workspaces/{ws}/events?after=<seq>&wait=<s>`.
* **Server-Sent Events**: `GET /v1/workspaces/{ws}/events/stream?types=...`. Each message carries `id: <seq>`, so a
  browser that reconnects sends `Last-Event-ID` and receives exactly what it missed, in order. Without a cursor the
  stream starts from now. Membership is re-checked on every batch, a keep-alive comment is sent every 15 s, a batch
  holds at most `limit` events (default 100), and the stream ends after `max_seconds` (default 300, at most 900) with
  an `end` message carrying the cursor, so connections are recycled without losing anything.
* **One event with its chain**: `GET /v1/workspaces/{ws}/events/{seq}` returns the event, every event sharing its
  correlation id that the caller may see, and each durable consumer's outcome (errors for admins only).
* **Webhooks** (unchanged): signed, retried, dead-lettered; see `docs/EXTENSIBILITY.md`.

The Streamlit UI is server-rendered, so it will not hold an SSE connection itself; phase 4 uses Streamlit fragments
that re-read the long-poll cursor. SSE is for the SDK, integrations and any browser client.

## Who sees what

`EventService.visible` is the one filter for every read path (list, stream, detail):

* Never another workspace's event, whatever the caller's role there. Reading or streaming a workspace you are not a
  member of is a 403 (`test_events_never_cross_workspaces_and_confidential_events_stay_with_admins`).
* Admins and owners see everything in their workspace.
* Below admin: no `confidential` or `restricted` event (even ids and counts), no `external.*` or `event.*` record,
  your own tasks and approvals only, and the shared change types in `MEMBER_TYPES`.

## Verified

`tests/test_event_engine.py` (11 tests) covers: a schema change as one versioned event committed with its version
(scenario A, the event half); duplicate emission and a re-published outbox row (G); retries then success, and a
redelivery skipped (at least once, idempotent); a poison event dead-lettered while the stream continues, then retried
by an admin; a rule loop stopped at the depth limit (L); a task's events continuing its trigger's chain; workspace
isolation and confidential filtering (K); SSE replay after reconnection with `Last-Event-ID`, a 403 for another
workspace's stream, integration events with dedupe and admin-only posting (J); a forward-only cursor (H at the
transport); and the envelope schema document.

## Rules and activities (phase 2)

Code: `src/lyst/autonomy.py`; routes in `src/lyst/api/events.py`; workspace migration 111. The rule engine is two
durable consumers on the event stream: `autonomy.rules` evaluates every active rule of the event's workspace, and
`autonomy.follow` verifies and delivers when a task an activity started ends.

### A rule

```json
{"name": "Investigate a revenue deviation", "on": ["external.finance.kpi"], "autonomy": "investigate",
 "when": {"all": [{"field": "subject.id", "op": "eq", "value": "revenue"},
                  {"field": "data.value", "op": "pct_change_gte", "value": 15, "other": "data.baseline"}]},
 "correlate": null, "suppress_seconds": 3600, "max_per_day": 20, "calendar": null, "priority": 6,
 "action": {"kind": "investigate", "question": "What was {subject.id} last quarter?"}}
```

* `when` is a tree of comparisons over named envelope fields (`type`, `source`, `sensitivity`, `actor`, `depth`,
  `subject.kind`, `subject.id`, `data.<key>`) with `eq ne gt gte lt lte in not_in contains exists missing
  pct_change_gte`, combined with `all`, `any`, `not`: at most 20 comparisons, 4 levels. There is no expression
  language and no user code; an unknown field is a validation error.
* `correlate` requires at least `min_count` events of a type within `within_seconds` (optionally on the same subject,
  and optionally only events that also meet its own `when`, so that three *failed* syncs are counted, not three syncs).
* `suppress_seconds` keeps a rule from acting on the same subject again within the window; `max_per_day` caps it;
  `calendar` limits it to weekdays and hours in a time zone.
* Placeholders in a question or summary are limited to `{subject.id}`, `{subject.kind}`, `{type}`, `{data.<key>}`.
* Every save is a new version (`event_rule_versions`), and a new version of an active rule returns it to draft.

`GET /v1/rule-templates` lists six starting rules (a failed data contract, a schema change, a broken artifact, a
recoverable task failure, a metric definition change, a failing connection). They are offered as drafts; none is
active until an admin turns it on.

### Governance

| Action | Risk | Runs without a person from | Owner needs |
| --- | --- | --- | --- |
| `notify` (inbox notice) | low | `prepare` | `workspace.read` |
| `impact_review` (dependents and repair proposals, from the catalog) | low | `prepare` | `data.read` |
| `investigate` (a read-only analysis task) | low | `investigate` | `task.create` |
| `retry_task` (one bounded retry of a recoverable failure) | medium | `automate` | `task.control` |
| `propose` (an asset repair or an investigation) | high | never: always an approval | `task.control` |

* The autonomy level (`observe`, `suggest`, `prepare`, `investigate`, `automate`) is a ceiling. Below an action's
  level the activity is a **suggestion** a person runs from Work (`POST .../activities/{id}/run`).
* A rule whose action is medium or high risk, or that runs at `automate`, needs a `rule.activate` approval before it
  is active; an approval for an older version does not activate a newer one.
* At execution the rule owner's permission is checked again, and an event about confidential data is acted on only
  for an owner who is an admin. A permission lost since activation blocks the activity (`blocked`), it does not run.
* `retry_task` retries only failures classified `transient`, `model` or `timeout`; the task contract's retry cap
  (`LYST_TASK_MAX_RETRIES`) still applies, and the template suppresses a second retry of the same task for a day.

### An activity

One per (rule, version, event): a redelivered event, or a replayed stream after the inbox was lost, never opens a
second one (`dedupe_key`). Stages follow **Detect → Interpret → Qualify → Plan → Authorize → Execute → Verify →
Deliver → Monitor**, each with a status and a detail drawn from records: the event, the condition's explanation, the
qualification decision, the plan, the permission or approval, the task or notice, the task's verification (every
error-severity check passed), the artifact kept, and the follow-up. Statuses: `observed`, `suggested`, `planned`,
`awaiting_approval`, `running`, `completed`, `needs_attention`, `failed`, `blocked`, `cancelled`.

`GET /v1/workspaces/{ws}/activities/{id}` returns the activity, its trigger event and its **flow** (trigger → rule →
decision → task → verification → artifact), all from stored records. Each status change is an `activity.changed`
event in the trigger's chain, so the stream (and the UI) sees it.

Suppressed, capped and chain-limited matches open no activity; they are kept in `rule_evaluations` with the reason
(`GET .../rules/{id}/evaluations`), 30 days.

### Dry run

`POST /v1/workspaces/{ws}/rules/simulate` evaluates a rule spec over the workspace's recent events, or over events
given in the request, and returns each decision with its reason and the counts. Nothing is run or recorded.

### Verified (phase 2)

`tests/test_autonomy.py` (10 tests): the rule representation refuses unknown fields, engine records and over-deep
trees, and explains a percentage deviation; a dry run of the failing-connection rule (scenario E logic) with
correlation and suppression; scenario A (a schema change opens an impact review listing the metrics computed from
the dataset, in the event's chain); scenarios B and C (a deviation beyond tolerance starts an investigation, the
activity completes with the verified briefing artifact, and the task's events continue the chain); scenarios D and F
(a stale report's repair waits for approval, then runs and keeps the previous version; a rejected one never runs);
bounded retry of a transient failure and none for an input error; scenario G at the rule layer; scenario L (rules
feeding each other stop at the chain limit); a suggestion run through the API, and cross-workspace denial.

## Anticipative events (phase 6)

* A monitor finding is staged as `metric.deviated` in the same transaction as the finding (kind, period, direction,
  z, value, baseline, and the investigation task if the monitor started one). The `metric_deviation` template
  investigates a validated shift or forecast deviation that has no investigation yet.
* Each new anticipation emits `anticipation.found` (admins only). A rule reacting to it can never start medium- or
  high-risk work by itself: the activity becomes a suggestion for a person. Whether anticipations were useful is
  still learned by anticipation itself (`anticipate/learning.py`).

## The activity UI (phases 4 and 5)

`src/lyst/ui/activity.py` is one kit every page uses, reading only the API:

* `presence(kind, id)`: one quiet chip when an object has open activity (working, needs approval, suggestion,
  problem); nothing otherwise. One cached workspace read however many objects a page shows.
* `panel` and `card`: what Lyst is doing about an object: the plain-words explanation, the recorded stages, the
  flow (event → rule → decision → work → check → result), an outcome preview (dependents, the report it kept), and
  the actions that apply (do it, approve, reject, try again, dismiss, open the result).
* `live()`: a fragment on every page (in the app shell) that reads the event cursor every 6 s. The cursor only
  moves forward, a repeated or older event is dropped, and only approvals, finished or failed activities and the
  person's own tasks raise a toast; everything else just drops the cached lists so the page re-reads the
  authoritative state. A new tab or a reload starts from now and shows current state.
* Where it appears: Work > Activity > Automatic (open activities first, then finished); a dataset's detail
  (presence next to its title, its activity at the top of the overview); a metric's detail in Brain; Settings >
  Workspace > Event rules (templates to add as drafts, dry run, turn on or off); in Analyst, a task Lyst started
  says so in one line with a Why button. Rule and activity approvals also appear, named in words, in Work > Needs you.

The Streamlit UI does not hold the SSE stream (Streamlit renders on the server); it uses the cursor API.

## Benchmark

`LYST_ENCRYPT=0 uv run python scripts/bench_events.py` (cloud container, 2 000 emissions from 8 threads, every
dedupe key sent twice, 6 active rules): about 125 emissions/s (p50 32 ms, p95 131 ms), exactly 1 000 events stored
(duplicates suppressed under contention), and about 64 deliveries/s through the two autonomy consumers, with 4 500
recorded evaluations and 300 activities. That is 2.6 times the first measurement (25/s), from:

* activity work moved out of dispatch (`autonomy.work`);
* inbox records and the cursor written once per 50 handled events, and one read of a page's inbox records;
* one redelivery check and one transaction per event for all of a workspace's rules;
* parsed rules reused for 2 s (changes made in the process clear them at once).

Enough for an analyst team's workspace; still not for a high-volume integration feed (see the risks).

## Risks and open work (prioritized)

1. **Throughput.** About 64 deliveries/s (benchmark above), bounded by DuckDB's single writer: each event still costs
   one workspace transaction when a rule matches. Evaluating a page of events per transaction, or sharding the
   dispatcher per workspace, are the next steps if a feed needs more. A crash now redelivers up to one batch (50
   events) per consumer; the inbox and the handlers' idempotency keys absorb that.
2. **Crash and connector coverage (scenarios E and I) is in-process only.** `tests/test_event_crash.py` kills a real
   dispatcher process (`os._exit`) before and after a rule's side effect; the restarted process redelivers the event
   and opens exactly one activity. `test_a_real_connection_that_keeps_failing_is_flagged_once` breaks a real SQLite
   connection: failed syncs are now audited (`connection.sync` with outcome `error`) and reach the stream, three in an
   hour make one notification, and later failures are suppressed. Neither runs against a networked source or across
   two hosts.
3. **Activity work is a separate job, in one process.** The rules consumer only opens an activity; `autonomy.work`
   runs planned activities on the worker tick (priority first, 50 per pass, a 20-second budget), so a slow or failing
   action never holds up the event stream. A process that dies mid-action leaves the activity planned and the next
   pass runs it again; every action is idempotent by its activity id (notice dedupe key, task idempotency key). Two
   Lyst processes would both run a planned activity (see item 5).
4. **Visual checks have no pixels.** `tests/test_ui_activity_visual.py` runs in CI without a browser: golden markup
   for the stage timeline and the flow (`tests/golden/activity/`, regenerated with `LYST_UPDATE_GOLDEN=1` after a
   review), every emitted class styled by the stylesheet every page loads, every colour token defined in both themes,
   every state mark at 3:1 or better on both backgrounds, and words for every state and stage. Layout (spacing,
   wrapping on a phone) is still checked by eye from screenshots.
5. **Single process.** Dispatch, the outbox collector and DuckDB's single writer assume one Lyst process. Running two
   API processes against the same home is not supported; a second process would collect and dispatch twice (the
   inbox would still prevent double handling, but not double collection races on the outbox stamp). A broker is not
   justified until Lyst runs as more than one process.
6. **Handler timeouts are not enforced.** Handlers run in the worker thread; a slow one delays others. Durations are
   recorded in `event_inbox.duration_ms`. Handlers should queue work (tasks) rather than do it.
7. **Outbox sweep cost** grows with the number of workspaces (one small query each, every 30 s).
8. **Per-subject ordering on retry.** A retried event can be handled after a newer one for the same subject (see above).
9. **Sensitivity is derived for dataset subjects only.** Other subjects default to `internal`.
10. **Retention.** Events are pruned after `LYST_RETENTION_DAYS_EVENTS` (90 days) with their inbox rows; published
   outbox rows after 7 days, which also bounds how long a staged dedupe key is remembered in the outbox (the stream
   remembers it for the event retention period).
