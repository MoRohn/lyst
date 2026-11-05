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

## Risks and open work (prioritized)

1. **Rules and activities are not built yet** (phases 2 and 3). Nothing yet decides what an event should cause; the
   engine only makes events durable, ordered, traceable and safely consumable.
2. **Single process.** Dispatch, the outbox collector and DuckDB's single writer assume one Lyst process. Running two
   API processes against the same home is not supported; a second process would collect and dispatch twice (the
   inbox would still prevent double handling, but not double collection races on the outbox stamp). A broker is not
   justified until Lyst runs as more than one process.
3. **Handler timeouts are not enforced.** Handlers run in the worker thread; a slow one delays others. Durations are
   recorded in `event_inbox.duration_ms`. Handlers should queue work (tasks) rather than do it.
4. **Outbox sweep cost** grows with the number of workspaces (one small query each, every 30 s).
5. **Per-subject ordering on retry.** A retried event can be handled after a newer one for the same subject (see above).
6. **Sensitivity is derived for dataset subjects only.** Other subjects default to `internal`.
7. **Retention.** Events are pruned after `LYST_RETENTION_DAYS_EVENTS` (90 days) with their inbox rows; published
   outbox rows after 7 days, which also bounds how long a staged dedupe key is remembered in the outbox (the stream
   remembers it for the event retention period).
