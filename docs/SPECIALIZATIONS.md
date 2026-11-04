# Analyst specializations and the Universal Analyst Core

One Lyst analyst, with expertise assembled per request from 136 specialization profiles. This file is the developer
and administrator reference and the honest register of what is real today.

## What exists (slice 1)

| Part | Where | State |
|---|---|---|
| Profile contract (typed, versioned, strict) | `src/lyst/specialists/schema.py` | Done. Pydantic, `extra="forbid"`, every section required and non-empty, 33 task classes. |
| 136 profiles, 11 domains | `src/lyst/specialists/catalog/*.json` | Done. Domain defaults plus per-role content, merged and validated at load. Each description is the entry from the Lyst Analyst Role Catalog (`docs/analyst_catalog.txt`), checked word for word by a test. |
| Shared capability library | `src/lyst/specialists/library.py` | 34 method families, each pointing at the real Lyst capability or function; status computed by resolving it. |
| Verification rules | `src/lyst/specialists/verification.py` | 36 rules; each says where it is enforced today, or that it is declared but not yet automated. |
| Maturity | `src/lyst/specialists/maturity.py` | Computed. Levels 3 to 5 need entries in `evidence.json`, which is empty. |
| Router | `src/lyst/specialists/router.py` | Layered routing (task intent, BM25 retrieval, workspace profile, tools, permissions, compatibility, cost, selection). |
| Composite assembly | `src/lyst/specialists/assembly.py` | Selected profiles merged into one execution with a bounded, untrusted-fenced brief. |
| Universal Analyst Core | `src/lyst/specialists/core.py` | `AnalystRequest` → `AnalystResponse`, one entry point for every channel; queues through the existing runtime. |
| API | `src/lyst/api/specialists.py` | Catalog, domains, library, profile detail and fit, the entry point, a task's expertise. |
| Routing benchmark | `src/lyst/specialists/bench.py` | 58 labelled cases plus 10 paraphrases, with a floor enforced in tests. |

Maturity today (computed on main at the time of writing): **101 Equipped (Level 2), 35 Defined (Level 1), none
Operational or higher.** Equipped means every capability the profile *requires* exists in Lyst and is fully
available. It does not mean Lyst can do the job: no representative tasks have been run and recorded per profile yet.
`GET /v1/specializations/overview` shows the live numbers and which missing capabilities block the most profiles
(model evaluation beyond forecasts, classification, log analysis, code analysis, two-source reconciliation,
geospatial, graph analysis, process mining, clustering, web research).

## The entry point (contract version 1)

Every channel, typed, voice, document, spreadsheet, image, audio, video, API or schedule, calls the same thing:

```
POST /v1/workspaces/{ws}/analyst/requests            (in process: lyst.analyst_core.handle(actor, AnalystRequest))

AnalystRequest
  workspace_id: str                 must match {ws}
  text: str (1..8000)               the request; for voice, the final transcript
  channel: "text"|"voice"|"document"|"spreadsheet"|"image"|"audio"|"video"|"api"|"schedule" = "text"
  prefer: [specialization id] (max 4)   perspectives the person asked for, e.g. ["cloud_finops_analyst"]
  attachments: [{kind: "dataset"|"document"|"artifact"|"file", ref: str}] (max 20)
  autonomy: str | null              as for tasks
  idempotency_key: str | null       as for tasks
  submit: bool = true               false: route and explain only, no task
  transcript_confidence: 0..1 | null    voice only; below 0.6 nothing starts and the response asks to confirm

AnalystResponse
  contract: "1"
  request_id: str
  status: "queued" | "routed" | "needs_confirmation"
  channel: str
  task_id: str | null               poll GET /v1/tasks/{id} exactly as for any task
  message: str                      one plain sentence a UI can show or a voice can speak
  expertise: {...}                  roles (id, name, level, why, authorized, blocked_permissions), methods,
                                    capabilities {usable, partial, missing, not_permitted}, verification
                                    {automated, not_yet_automated}, approvals, outputs, sensitivity, limitations
  routing: {...}                    task_classes, selected and considered candidates with scores and reasons,
                                    overrides, unresolved_overrides, independent_review, general, unsupported,
                                    catalog_version, latency_ms
  limitations: [str]
```

Guarantees: routing depends only on the text, the stated preference, the actor's permissions and the workspace's
business profile, never on the channel, so a spoken and a typed request select the same expertise (tested). The
task runs through the unchanged durable runtime; the assembled expertise is stored at `request.expertise` and in a
task event of kind `expertise`. `GET /v1/tasks/{id}/expertise` reads it back.

## Security model

- Profiles and custom profiles are untrusted configuration. The router only scores their text; the only part that
  can reach a model is the brief, which is capped at 1,800 characters and wrapped by `security.untrusted`.
- Role selection never grants anything. Each selected discipline's required permissions are checked with
  `guard.can` for the actor; missing ones are reported (`blocked_permissions`) and the capabilities behind them are
  listed as `not_permitted`. The runtime still checks every step's permission as before.
- Unknown capabilities, verification rules or permissions in a profile fail catalog load.
- Specialization metadata never changes system instructions; there is no field that could.

## Adding or changing a profile

Edit the domain file in `catalog/`. Role keys are short names (`desc`, `resp`, `know`, `terms`, `methods`, `tasks`,
`techniques`, `req`, `opt`, `sources`, `entities`, `fields`, `verify`, `outputs`, `limits`, `examples`, `related`,
`keywords`, `perms`, `sensitivity`, `depth`) or full `section.field` paths; anything else is rejected. Verification
rules, limitations, permissions and technical concepts add to the domain defaults; other fields replace them. Run
`uv run pytest tests/test_specialists.py`; the benchmark floor catches routing regressions.

## Known limitations of this slice

- **Routing accuracy is measured on the author's own cases.** 58 of 58 labelled cases route correctly. The 10
  paraphrases first scored 5 of 10; the misses were fixed by adding vocabulary, so that set is no longer unseen. An
  independent set written by someone else is the next honest measurement.
- The engine does not yet *use* the assembly inside Plan and Verify: the expertise is recorded on the task, but the
  plan, the validation checks and the answer are the same as without it. Wiring verification rules into `validate`
  and the brief into the answer is slice 2.
- No profile is Operational: no per-profile representative tasks have been executed and recorded.
- Custom workspace profiles, the Foundry hook for gaps, anticipation links and the UI surfaces are later slices.

## Backlog (in order)

1. Engine integration: the assembly's verification rules drive extra checks in `validate`; declared-but-unautomated
   rules are disclosed in the answer; the brief reaches the narrative step.
2. Operational evidence: representative scenarios per equipped profile on the demo data, recorded in
   `evidence.json` by a script, so Level 3 is earned per profile.
3. Workspace custom profiles (workspace migration 70), admin-only, versioned, validated against the same schema and
   reference checks, never able to add permissions.
4. Foundry hook: an `unsupported` task class becomes a drafted, tested, approval-gated capability proposal.
5. Anticipation: event kinds in `events.CATALOG` mapped to the disciplines that should prepare work.
6. UI: Specializations catalog under Brain, custom profiles under Settings (UX thread).
7. Missing capability families, highest leverage first: model evaluation, classification, log analysis.
