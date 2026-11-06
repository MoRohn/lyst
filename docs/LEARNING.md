# Learning loop

How Lyst gets better from use, what it learns from, and what it never does on its own. The loop is measurable and
governed: every change to what Lyst reuses is recorded, has a reason, and is either automatic and undoable (holding
something back) or a person's decision (promoting something). Nothing here edits code, prompts or harness rules.

## What Lyst learns from

| Signal | Where it is recorded | What it changes | Who decides |
|---|---|---|---|
| A verified answer | `analyses`, an `observed` episodic memory | Recalled as context next time; compiles a reusable workflow (`docs/ARCHITECTURE.md`, `lyst.workflows`) | Lyst's own checks |
| A person's verdict on an answer | `answer_feedback` (migration 120) | See below | The person (editor or above to change reuse) |
| A correction | a `user_validated` memory linked to the analysis | Recalled ahead of `observed` memory | The person (editor or above) |
| Repeated failures of one job | `harness_failures` | A proposed harness rule | An admin approves (`docs/HARNESS.md`) |
| What people do with anticipations | `anticipation_events` | Ranking weights per family | Automatic, bounded (`docs/ANTICIPATION.md`) |
| A graph edge failing twice | `graph_rules` | A learned constraint on the Planner | `docs/GRAPH_RUNS.md` |

## A verdict on an answer

`POST /v1/workspaces/{ws}/analyses/{analysis_id}/feedback` with `{"verdict": "correct" | "incorrect" | "partly",
"note"?, "correction"?}`. One standing verdict per person and analysis: a new one replaces the earlier one.

- **Incorrect** disputes the analysis. Its episodic memory leaves recall (an approved memory stays; an admin decides),
  it is no longer cited as a prior answer, and the compiled workflow it ran on or verified goes stale with the reason
  "a person marked an answer it gave as incorrect (analysis ...)". The next question is planned afresh. A verified run
  with the *same steps on the same data* is not recompiled (the compile result says `blocked`); a different plan, or
  new data or definitions, compiles a new version as usual.
- **Correct** confirms the analysis and raises its episodic memory from `observed` to `user_validated`.
- **Partly** is recorded only.
- A **correction** is stored as a `user_validated` team memory linked to the analysis.

A viewer's verdict is recorded and counted (`GET /v1/workspaces/{ws}/feedback/summary`) but changes nothing Lyst
reuses: holding back a workflow others rely on needs at least an editor.

**Undo.** `POST /v1/workspaces/{ws}/feedback/{id}/withdraw` (the person who gave it, or an admin) undoes its effects. A
workflow another standing dispute still holds stays stale. A restored workflow is re-checked against its data and
definitions before any reuse, as always.

**Crash safety.** Effects are reconciled from the verdicts still standing (`FeedbackService.reconcile`), not applied one
step at a time, so a crash between the verdict and its effects is repaired by the next verdict, withdrawal or reconcile,
and reconciling twice changes nothing (`tests/test_learning.py`).

## Limits

- A dispute holds back reuse; it does not make the planner find the right answer. The correction, recalled as context,
  is what informs the next run.
- Verdicts are not yet shown in the UI; the API is the way in.
