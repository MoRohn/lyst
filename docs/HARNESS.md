# The task harness

The model reasons and proposes. The harness around it decides what it sees, what it may touch, what survives between
sessions, what counts as evidence and when a run must stop. Most of Lyst's harness was already in the runtime; this
page maps it, and describes the four pieces added on top of it: a contract per task, a change receipt per run, failure
classes that propose permanent harness rules, and a durable state view.

## The seven jobs, mapped to Lyst

| Job | Where it lives | Added here |
|---|---|---|
| 1. Turn the request into a contract | `harness.build_contract`, written with the task in one transaction | **new** |
| 2. Give the agent a map | `contextualize` selects the metric definitions, up to 5 memory entries and 3 document passages the question needs; `AGENTS.md` for coding agents | `AGENTS.md` **new** |
| 3. The right tools in the right environment | `analyst/registry.py` `Capability` (permission, phase, mutating, requires_approval, optional); the read-only SQL guard; the workbench sandbox | tool routes from rules |
| 4. Externalize memory into durable state | step checkpoints in `task_steps`, `memory`, approvals, decision records | `GET /v1/tasks/{id}/state` **new** |
| 5. Sensors before autonomy | `validate` checks, the evidence graph, data contracts, the evaluation gate | contract `done_when` checks |
| 6. Permissions outside the model | `Guard.require`, data policy, `_approval_gate`, graph lanes | permission gates from rules |
| 7. Traces and local recovery | `GET /v1/tasks/{id}/trace`, leases, takeovers, `recover_orphans`, retry from the failed step | receipts **new** |

## Contract

Every task is submitted with a frozen contract, hashed with sha256. `GET /v1/tasks/{id}/contract` returns it and,
once a run has ended, how each check came out.

```json
{
  "goal": "What was revenue last quarter?",
  "job": "what was revenue last quarter",
  "kind": "analysis",
  "inputs": ["workspace:northwind"],
  "output": "a verified answer with its evidence",
  "constraints": {"autonomy": "delegate", "max_steps": 40, "budget_seconds": 600, "budget_usd": 1.0,
                  "max_retries": 3, "approval_required_for": [], "refused_tools": [], "decisions": [], "guides": []},
  "done_when": ["steps_finished", "within_budget", "goal_kept", "outcome_complete", "validation_passed"]
}
```

Each `done_when` item is evaluated from stored records (the task, its steps, its result, the model-call log), never
from the model's own status:

| Check | Passes when |
|---|---|
| `steps_finished` | the task is done and every step finished (an optional step may be skipped) |
| `within_budget` | no step, time or cost budget stopped it, and spend is under `budget_usd` |
| `goal_kept` | the question in the result is the question asked (protects against silent redefinition) |
| `outcome_complete` | the result is `complete`, not `unverified` or `needs_input` |
| `validation_passed` | no validation check failed |
| `has_sources` | the answer lists its dataset versions and queries (added by a test rule) |
| `no_validation_warnings` | no validation check warned (added by a test rule) |
| `check:<name>` | that named validation check ran and passed (added by a test rule) |

A run that finishes with its contract unmet keeps status `done` (the run ended) and records a `contract_missed`
event; its receipt says `met: false`. If the stored contract is edited afterwards its hash no longer matches and the
contract reads `intact: false`.

## Bounded loops

- **Steps** and **time**: as before (`max_steps`, `budget_seconds`, also while a step runs).
- **Cost**: new. `budget_usd` caps model spend per task (`LYST_TASK_BUDGET_USD`, default 1.00, 0 for no cap). It is
  checked between waves against the model-call log; a run over it ends through `_fail`.
- **Retries**: a step retries a transient failure once (`MAX_STEP_ATTEMPTS`); a person may retry a failed task up to
  `max_retries` times (`LYST_TASK_MAX_RETRIES`, default 3). Past that the same request is refused: change it or ask
  again. Worker takeovers stay capped by `LYST_TASK_MAX_TAKEOVERS`. Graph branches stop after `max_attempts`.

## Change receipt

Every run that ends (done, failed or cancelled, including a step a person rejected) leaves one immutable receipt,
numbered per task (`GET /v1/tasks/{id}/receipts`):

| Field | From |
|---|---|
| `contract` | its hash, whether it is intact, whether it was met, and each check with its evidence |
| `context_sources` | the metric definitions and dataset versions, memory entries and document passages context selected |
| `policy_version` | the workspace data-policy version and a fingerprint of the active harness rules, plus the rules applied |
| `model_route` | each workload and the model that served it |
| `tools_used` | the capabilities that ran |
| `checks` | validation pass, warn and fail counts; contract checks passed and failed |
| `retries` | model-call retries, task attempts, worker takeovers, person retries |
| `human_corrections` | approvals decided and retries requested |
| `cost_usd`, `tokens`, `active_seconds` | the model-call log and the runtime clock |
| `accepted_artifact` | the result, only when the contract was met |
| `rollback_point` | what the run changed (mutating capabilities), the dataset versions it read, and how to restore |

Two receipts with the same `policy_version` ran under the same harness, so a model change shows up in `model_route`
and its effect in `checks`.

## Failure becomes infrastructure

A run that failed, missed its contract, or had a step rejected is classified and recorded
(`GET /v1/workspaces/{ws}/harness/failures`):

| Class | Recognised by | Proposed harness change |
|---|---|---|
| missing context | an input or data error naming something not found or undefined; an answer to a different question | **guide**: a note stored in workspace memory, which context recalls for matching questions |
| wrong tool | an input or data error from a capability | **tool**: that capability is not used for this job; the run stops at once with the rule's reason |
| bad output | finished, but a contract check failed (unverified, failed validation) | **test**: stricter `done_when` for this job (`has_sources`, the named validation checks) |
| repeated loop | a step, time or cost budget stopped it, or its worker died repeatedly | **budget**: half the time budget and no retries; a person changes the request |
| unsafe action | a person rejected a step, or a step was refused permission | **permission**: that step waits for a person on this job in every autonomy mode, schedules included |
| lost decision | the task stopped to ask the person | **state**: a decision stored in workspace memory |
| unknown | failed with no recorded step error | nothing proposed; the trace and receipt are the evidence |

Transient and model-outage failures teach nothing (the runtime already retried them) and are not recorded.

When the same failure (same job, class, capability and normalised message) happens twice
(`LYST_HARNESS_PROPOSE_AFTER`, default 2; a rejected step is enough once), a rule is **proposed**: an approval
`harness.update` goes to the workspace's approvers and an inbox notice is raised. Nothing changes until a person
approves it. An approver can reword it first (`PUT /v1/workspaces/{ws}/harness/rules/{id}`). Once approved, the rule
is **active** and applies mechanically to every later run of that job: it is written into the new task's contract
(`rules_applied`) and enforced by the runtime. A rejected rule is never proposed again for the same failure. Admins
retire a rule (`POST .../harness/rules/{id}/retire`), which also revokes the memory entry a guide or decision wrote.

## Durable state

`GET /v1/tasks/{id}/state` rebuilds what the next session needs from records, not from a conversation: the current
step, finished and failed steps, the contract, artifacts, decisions (approvals and recorded decisions), failures,
pending approvals, each budget used against its limit, and where and how it would resume. Finished steps are never
re-run: a paused task resumes, a failed one retries from the failed step, and a dead worker's task is taken over.

## The checklist

`GET /v1/workspaces/{ws}/harness` (admins) answers the harness checklist for the workspace from its own records,
each item as yes, partly or no with the evidence counted and where the mechanism lives. Two items are honestly
**partly**: execution is isolated by the SQL guard and the sandbox but there is no separate production copy of the
data, and every change is versioned but restoring one is a person's step, not automatic.

## Rules written twice

`AGENTS.md` is the map for coding agents. Its important rules are also checks in `tests/test_harness_boundaries.py`:
the UI never imports the store, runtime, data or security layers; the backend never imports the UI; migration
versions are never reused and the harness lane stays in 100-109; the run loop ends tasks only through `_fail` and
`_finish`; and every path the map names exists.

## Limits

- A job is the normalised question text. Two wordings of the same request are two jobs, so a rule learned on one
  does not apply to the other.
- Proposals are drafted from the evidence by templates, not by a model; the approver is expected to reword guides
  and decisions.
- A tool rule routes a job away from a capability; it does not choose a different one. The run stops with the
  reason, and the person rewords the request.
- The receipt names the rollback point; it does not roll anything back.
- Tasks submitted before this change have no contract and get no receipt.

## For the UI threads

No UI is included. Suggested surfaces: on a task, a **Contract** panel (goal, constraints, each check with its
evidence) and a **Receipt** panel per run; in Settings > Advanced, a **Harness** section with the checklist and the
rules (proposed rules link to their approval, with an edit box, and active ones with a retire button); in the approval
inbox, `harness.update` approvals show the rule text and the failures that led to it.
