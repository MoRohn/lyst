# Graph runs and the Brain graph

A loop makes one unit of work correct without a person. A graph decides which units exist, what runs at the same
time, what waits, and where a person signs. Lyst's graph runs put both in the Brain, so a goal can run from open to
done and the person approves one step: the one that cannot be undone.

## The nodes

| Node | Job | In | Out | Fails as |
|---|---|---|---|---|
| Planner (splitter) | cut the goal into branches; apply learned constraints | goal, intake, rules | branches with lane and inputs, requirements | no branch, never-touch |
| Builder (worker) | do one branch as an ordinary Lyst task, in its own context | job, inputs, returned packet | task, result, analysis record | start failed, task failed |
| Checker | grade the stored result against each requirement | task, analysis record, requirements | requirement, passed, evidence, strength | requirement failed |
| Gate · Router | gate on blast radius; route pass, repair, escalate or stop | checks, lane, trajectory, history | next node, packet | blocked, held |
| Merge (code) | deduplicate and order the passed answers; no model | passed branches | answer, branch, proof | nothing to merge |

The Planner splits only what was written (one branch per line or `;`) and never invents work. Branches run at the
same time unless one names another's answer (`{b1}`): sequence is not dependency, so "and then" is not an edge.

## The check comes first

Every requirement is one a program can fail. Evidence is ranked, and noise never passes a unit:

- **Strong**: the result matches its contract (a complete answer), the data's must-pass validation checks pass, a
  phrase the person required is present.
- **Medium**: the answer carries its source (dataset versions with their sha256 and the queries, as the analysis
  recorded them), and an independent path agrees (the Checker re-runs the recorded queries itself, as the run's owner,
  through the same governed SQL, and compares their shape).
- **Weak**: a reviewer agent approves. Not used as a gate.
- **Noise**: the model says it is confident; it looks good; no errors were thrown. Never a gate.

No node grades its own work: the Checker reads the stored task and analysis record, never the Builder's own status.

## Failure policy, decided before the run

| When | Action |
|---|---|
| tool or network blip | retry; nothing is learned |
| model or source down | retry; the model gateway already falls back across models |
| optional branch failed | skip; the run does not count it as a failure |
| output failed its check | repair that unit only, with a packet: unit, verdict, reason, evidence, scope |
| 3 repairs failed | escalate: blocked, admins told; the plan is wrong, not the unit |
| budget or permission hit | stop: blocked at once, a person woken |

Only the failing unit goes back; the others are never rebuilt. Stop rules: done (every requirement has evidence), cap
(max attempts), budget (minutes for the whole run), memory (every attempt, packet and rule is kept, rejects included),
escape (an inbox notice to admins and the run's owner).

## Lanes: blast radius, not confidence

- **Contained** (reading and analysing): passes on its checks.
- **Wide** (defining, changing, sharing): also needs a clean trajectory in this run and no failures in earlier runs,
  or a person accepts it.
- **Closed** (deleting, sending, paying, deploying, production, or anything named at intake as needing approval): never
  starts without a person's approval, and the person who planned the run cannot open their own closed lane when
  another approver exists. There is no threshold to lower.

## Two return paths

The correction edge is short: one unit back to its Builder, fixing this run. The learning edge is long and lands in
the Planner's brief, fixing every later run:

- **Avoid**: when the same edge fails twice for the same reason, a rule is written. A later plan with that job is
  flagged, and stops on its first repeat of that failure instead of spending its retries.
- **Rewrite**: when a person's fix for a blocked branch passes, the Planner plans the fix in place of the job that
  failed, and says so on the plan.

Admins can retire a rule. Reruns keep the graph (the same split), so what it learned carries over.

## What a run leaves

- `state.json`: the run, each branch with requirements, evidence, attempts and packets, and every edge as it ran.
- `checks.md`: one row per requirement with its evidence.
- `graph.md`: the graph as a Mermaid flowchart, drawn from the edges taken.
- A final check (did every branch end in pass, blocked or skipped; does every passed requirement have evidence; wall
  clock against the same work as a chain) ending in "What the graph learned".
- The morning brief (at most 15 lines: done with proof, blocked and why, the graph as it ran, what waits on approval)
  and the weekly review (which nodes failed most, which edges can merge, one change to make the graph faster).

## Brain › Graph

The Brain opens on its graph. **Map** shows every dataset, approved metric, business term, document, memory,
business entity, analysis and artifact, plus graph runs, branches and learned rules. A link is a relation Lyst recorded
or a literal mention, never an inferred one. Colours: the accent for metrics, neutral for datasets and documents, and
the status colours only as health rings. Search, zoom, pan, drag, hide a kind from the legend, and the **List** view
for the keyboard; under prefers-reduced-motion the layout is computed once and nothing moves. Clicking a node opens a
side panel with its backlinks and opens the existing view (a dataset in Data, a metric's definition, an analysis where
it can be continued, a run in Runs). **Runs** plans, starts and follows graph runs with the flow drawn as it ran.
**Harness** shows the rules every node re-reads.

## Limits, stated plainly

- Builders are Lyst analyses. A graph run cannot do anything a person's own question could not, and it does not write
  code, deploy or move money.
- The Planner splits text; it does not decompose a goal with a model.
- "Independent path" re-runs the recorded queries; it does not derive the answer a second way.
- The morning brief and weekly review are produced on request; they are not yet scheduled for a time of day.
