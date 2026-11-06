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

## Slice 2: the run uses the assembly (`specialists/applied.py`)

Tasks routed through the core carry `request.expertise`. Three engine touch points read it; tasks without it run
exactly as before (tested):

- **Plan**: `validate` runs at high intensity (independent recompute in Python) when any assembled discipline
  requires an independent recompute or deep planning. This also applies to compiled workflows. Expertise can only
  tighten verification, never relax it.
- **Validate**: `expertise_rules` lists every rule the disciplines require with a status: `passed` / `failed` (from
  the checks that ran, with their detail), `enforced` (held by the SQL guard, delivery or contextualize on every
  task), `not_applicable` (the answer did not involve it), or `not_automated`.
- **Deliver**: `supporting.expertise` names the disciplines and the rule results; when a required rule is not
  automated, the answer's assumptions say so in plain words ("This kind of analysis also calls for checks Lyst does
  not run automatically yet: ..."). The note contains no digits, so it can never trip the unbacked-number check.

## Slice 3: real methods, earned evidence, an honest audit

**Audit** (`specialists/audit.py`, `scripts/audit_specializations.py --write` → `docs/SPECIALIZATIONS_AUDIT.md`,
`docs/quality/specializations-audit.json`). Every profile's example requests are routed and then run to the end. On
the demo workspace before this slice: 268 of 272 routed to their own profile, only 9 produced a verified answer, 257
were declined (no metric or data for the discipline), and several were "answered" with an unrelated metric.

**Fit check** (`applied.fit`, check `fits_request` in `validate`). A routed request whose specific task classes the
executed plan does not address is no longer presented as verified: "Is our demand model drifting?" used to return
a support-ticket total as a complete answer; it now reads "Not verified: the request asks to find observations that
depart from expected behaviour ...". Unrouted tasks are unchanged.

**Specialist methods** (`analytics/methods.py`, `specialists/methods.py`, capability `run_method`). Seven standard
methods: k-means segmentation (silhouette-chosen k), logistic classification with a time-ordered holdout, process
mining (variants, handoff waits, rework, cycle times), great-circle distances, two-source reconciliation, graph
components and degree, event-log bursts against each entity's own baseline. A routed request that no governed
metric covers runs one when (a) the request asks for it in words and (b) it names the dataset: every distinctive
word of the dataset's name must appear ("login events" names `login_events`, "events" does not). An earlier draft
that also used an unnamed dataset whose columns happened to fit produced confident answers on the wrong data in the
audit (graph statistics for a vulnerability question); that path was removed. Every read goes through
`DataService.query`: SQL guard, row and column policies, governance access checks (tested with a restricted
dataset), audit. `run_method` is in the capability registry, so skill discovery lists it.

**Samples and evidence** (`specialists/samples.py`, `representative.py`, `scripts/evidence_specializations.py --write`
→ `evidence.json`). Synthetic datasets with planted answers (a slow approval step, one account's login burst, two
stores far from any warehouse, three ledger breaks, two supplier groups, three customer segments, churn driven by
support tickets). Thirteen representative scenarios check the *content* of the answer, not only that it finished.
All thirteen pass; the test suite re-runs them and fails if one breaks or if `evidence.json` disagrees.

Maturity now: **8 Operational** (data, BI, data quality, statistical, financial data, financial systems, process
mining, CRM; computed live), 98 Equipped, 30 Defined. Five disciplines have passing evidence but stay at Level 1
because a capability they require is only partial: forecasting and predictive analytics (model evaluation covers
forecasts and Lyst's own answers only; classification is binary logistic only), cybersecurity and network (tabular
logs only), geospatial (points only). A computed level never ignores that.

Audit after this slice: see `docs/SPECIALIZATIONS_AUDIT.md`. Most example requests are still declined, correctly:
they ask about data the demo workspace does not have (CI runs, cloud bills, scanner exports). The honest next step
per discipline is data, not more profile text.

## Slice 4: baked into every page and activity, and earned across 108 roles

**Every entry point.** Seventeen places submit tasks (the agent bar on every page, the activity board, Home, schedules,
follow-ups, anticipation, monitors, onboarding, assets, MCP, voice, the tasks API...). Only the core's own endpoint
used to route. `TaskRuntime` now has `before_submit` / `after_submit` hooks, and the lane attaches
`UniversalAnalystCore.enrich` / `record`: every task gets assembled expertise (same router, same permission rules) and
an `expertise` trace event, whatever submitted it. Platform upkeep kinds (`maintenance`, `system`, `sync`, `refresh`)
are never routed; a submission can opt out with `extra={"expertise": None}`; a failing hook never blocks a task.
`runtime.list` returns `specialist` (the primary discipline's name) and the activity board shows it on each card.
Because every task is now routed: an explicit request for a quick answer is never raised to high verification, and
the fit check only judges bare number answers (value, breakdown, trend), where the audit found wrong-question answers.

**Data and methods so each role can be exercised.**
- `packs.py`: 55 domain sample packs (cloud billing, GPU fleet, clusters, storage, incidents, service desk, CI,
  deployments, APIs, product usage, web traffic, SEO, ad spend, subscriptions, pipeline, churn, social, content,
  video, podcasts, procurement, shipments, inventory, attrition, scrap, sensors, energy, emissions, readmissions,
  cases, robots, vulnerabilities, posture, access grants, chargebacks, model spend, agent failures, chatbot
  escalations, RPA exceptions...), each a governed metric with one member planted as the cause of last month's move.
- `samples.py`: plus A/B and holdout experiments, a clinical trial, recorded predictions and five pass/fail
  evaluation sets with a planted weakest segment, and 18 short documents (policies, reviews, registers, notes) that
  each state one checkable fact.
- New methods: `compare_groups` (two-proportion z-test or Welch, lift with interval, sample-ratio check),
  `evaluate_predictions` (accuracy, per-class precision/recall, regression error, pass rates by segment) and `brief`
  (an extractive brief quoting the matching document, chosen by title or passage, role-gated). Model evaluation is now
  `available`.

**Evidence.** `representative.all_scenarios()` holds a task with a known answer for 112 roles, and all 112 pass. A role is Operational
only if it is Equipped *and* its task selects it, verifies, and names the planted fact. Result: **108 Operational,
5 Equipped, 23 Defined**. Four roles have passing tasks but stay below Level 3 because a capability they require is
only partial: predictive analytics (binary classification only), geospatial (points only), cybersecurity and network
(tabular logs only).

What "Operational" means here, plainly: the role's representative task runs end to end on controlled sample data and
finds the right answer. It does not mean the role is validated on real data or that every request in that discipline
will work: the methods are the ones listed in the library, and a workspace still needs its own data and metrics.

## Slice 5: the remaining roles

New methods, each in `analytics/methods.py` behind `run_method`:
- **Raw logs**: `parse_logs` reads JSON lines, key=value, web access and syslog formats (unmatched lines are counted,
  never guessed); `log_patterns` adds repeated-failures-then-success sequences and a timeline for the entity.
- **Graphs**: Brandes betweenness (brokers), deterministic label-propagation communities and shortest paths named in
  the request.
- **Classification**: multinomial logistic regression for 3 to 12 classes, per-class precision and recall.
- **Optimization**: exact integer or linear programs for budget allocation (SciPy HiGHS `milp`), budget read from the
  request, binding limits reported.
- **Text**: TF-IDF themes, word-list sentiment by group, simple entities.

Library: 28 available, 4 partial (data cleaning, regression, geospatial, concentration), 2 missing (code analysis,
web research). Samples add raw auth and firewall logs, service logs, wallet transfers, service dependencies, ticket
comments, channel options, maintenance history, gene expression, detection results and two predicted-vs-measured
sets, each with a planted answer. Nineteen more representative tasks pass.

**Maturity now: 127 Operational, 1 Equipped, 8 Defined.** What is left and why:
- Software systems, technical debt, application security, smart contract security: need source-code analysis that
  runs tests and reads traces; Lyst has none, so they stay Defined.
- Geospatial and transportation: point distances only; no polygons, regions or travel-time routing.
- Analytics engineer: cleaning is SQL transforms only; no imputation or fuzzy matching that writes a cleaned dataset.
- Digital forensics and OSINT: needs external research, which Lyst does not do by design.
- Quantitative research: Equipped, but no representative task yet that tests regimes rather than reporting a change.

## Known limitations

- **Routing accuracy is measured on the author's own cases.** 58 of 58 labelled cases route correctly. The 10
  paraphrases first scored 5 of 10; the misses were fixed by adding vocabulary, so that set is no longer unseen. An
  independent set written by someone else is the next honest measurement.
- The assembly changes verification and disclosure, not the computation: the plan's capabilities are still chosen
  by the existing engine, so a specialized request gets the same figures as a plain one, checked harder and
  reported more honestly (slice 2, below). Choosing different methods per discipline is later work.
- 9 roles are below Operational (listed in slice 5) because a required method is missing or partial.
- Document briefs are extractive: they quote the best passage; they do not reason across documents.
- The engine's metric matching sometimes maps a word to a governed metric loosely (for example "peak demand" to
  support tickets); the fit check catches some of these, not all.
- Custom workspace profiles, the Foundry hook for gaps, anticipation links and the UI surfaces are later slices.

## Backlog (in order)

1. Done in slice 2, except the brief reaching the narrative step and per-discipline method choice.
2. Operational evidence: started in slice 3 (13 disciplines). Next: scenarios for the remaining equipped profiles,
   each with sample data that has a known answer.
3. Workspace custom profiles (workspace migration 70), admin-only, versioned, validated against the same schema and
   reference checks, never able to add permissions.
4. Foundry hook: an `unsupported` task class becomes a drafted, tested, approval-gated capability proposal.
5. Anticipation: event kinds in `events.CATALOG` mapped to the disciplines that should prepare work.
6. UI: Specializations catalog under Brain, custom profiles under Settings (UX thread).
7. Missing capability families, highest leverage first: model evaluation, classification, log analysis.
