# Lyst UX strategy: intuitive for business users, just as capable for experts

Status: 2026-11-04. Part A (low-regret) and Part B (decided by Rohn) are implemented in PR #22. Screenshots: `docs/ux/before/` (main at a28e200) and `docs/ux/after/` (this branch).

## 1. Who we design for

| Person | Typical question | What they need from Lyst | What gets in their way today |
|---|---|---|---|
| Finance lead | "Why did operating margin fall last quarter?" | The answer, the driver, whether to trust it, a file to forward | Opens at Analyst depth with decomposition tables; "rate effect", "mix effect", "-0%" |
| Ops manager | "Tell me if support tickets spike" | Set it once and be told | Has to find Work › Recurring, then a popover with a σ slider |
| Executive | "What should I do about EMEA?" | Two lines and one decision | Must know the depth control exists and that "Executive" is a view, not a role |
| Analyst / engineer | "Show me the SQL and the checks" | Everything, one click away | Nothing; keep it |

The north star stays: one analyst, tenfold capability. For a business user that means **ask → trusted answer →
act**, with every expert capability one deliberate click away, never in the way.

## 2. Audit: concrete friction (business user's seat)

Walked on the Northwind demo, desktop 1440 and phone 390. Severity: H blocks or misleads, M slows, L polish.

| # | Where | Friction | Sev |
|---|---|---|---|
| F1 | First run | Nothing tells a new person what to do first or what "done" looks like; Home is a text box and five chips | H |
| F2 | Answer | Owners and admins open every answer at **Analyst** depth (tables, hypotheses, assumptions); most business owners are admins | H |
| F3 | Answer | The recommended actions say "set a monitor on operating margin" but the answer offers no way to do it; acting means leaving for Work › Recurring | H |
| F4 | Answer | "Verified" is a badge with no explanation of what was checked | M |
| F5 | Answer | Table headers in code case (`region`), negative zero (`-0%`), "Rate effect / Mix effect" unexplained | M |
| F6 | Answer | Four download buttons plus "How this ran" in one row, Markdown beside PDF; reads as a toolbar for engineers | M |
| F7 | Answer | "Verified, so Lyst saved this as a reusable workflow (Build › Workflows)": engineering vocabulary on the main path | M |
| F8 | Work › Recurring | "Alert when a change is beyond (σ)" slider; "Monitors", "Schedules", "Priority 3.2" | H |
| F9 | Work › Needs you | Approval rows show the raw action id (`schedule.create`) | M |
| F10 | Work › Details | Steps listed as `capability · agent` identifiers; "autonomy assist" | L |
| F11 | Data | Rows lead with a code name chip (`pnl`), "v1", lowercase classification | L |
| F12 | Intelligence | Metric rows end in a dataset code (`· pnl`); the metric dialog opens on SQL | M |
| F13 | Build | Dashboards (business) share a page with Python skills, a capabilities table and evaluation runs (engineering), and the page opens on Workflows | M |
| F14 | Navigation | "Intelligence" and "Build" do not say what is inside; "Work" is close | M |
| F15 | Everywhere | No glossary; terms like governed, workflow, skill, monitor, autonomy have no plain definition in the product | M |
| F16 | Home | "governed data" in the subtitle; the capability line lists metric titles without saying what you can do with them | L |

What already works and must be kept: one navigation (PR #4), list rows instead of tables, one primary action per
view, plain progress phases, "Needs you" first, caveats visible at every depth, URL-addressable sections.

## 3. Principles

1. **Answer first, method on request.** Every screen leads with the business conclusion; how it was computed is one
   click away and never removed.
2. **Every recommendation is a button.** If Lyst says "watch this" or "repeat this", the answer offers it in place.
3. **Business words in the interface, system words in the method.** The glossary (section 6) is the contract;
   identifiers (`pnl`, `schedule.create`, σ) appear only inside method, SQL or Settings.
4. **Trust is explained, not asserted.** A badge always says what it means in one line.
5. **Progressive disclosure by depth, not by role.** Everyone starts at the business depth; the depth a person
   picks is remembered. Experts lose nothing.
6. **One place per job.** No new areas, no duplicated screens (Rohn: the UI was "much too fragmented").
7. **A first run that teaches by doing.** A short checklist on Home that disappears when done.

## 4. The primary flow: ask → verified answer → act

```
Home ─ ask box + suggested questions + "Get started" (first run only)
  │  ask (or pick a suggestion)
  ▼
Progress ─ plain phases: "Finding definitions and data · 3 of 9 steps" · Stop
  ▼
Answer (Business depth by default)
  ├─ Headline answer, Verified badge + one-line meaning ("All 7 checks passed: every figure recomputed…")
  ├─ Caveats (always)
  ├─ Evidence · chart · What it means · What to do
  ├─ Act bar:  [Watch this metric]  [Repeat it ▾ weekly/monthly]  [Download ▾ PDF · Slides · Excel · Markdown]  How this ran
  ├─ Follow-up suggestions
  └─ Depth: Business · Analyst · Engineer · Executive  (Part B proposes plainer names)
```

"Watch this metric" proposes a monitor on the answer's metric (an admin approves, as today). "Repeat it" schedules
the saved, verified analysis for each new period when one exists, or the question itself otherwise. Both reuse
existing API endpoints; nothing new in the engine.

## 5. Default versus expanded

| Surface | A business user sees by default | An expert expands to |
|---|---|---|
| Answer | Headline, trust line, caveats, evidence, one chart, what it means, what to do, act bar | Analyst: tables, hypotheses, assumptions; Engineer: plan, checks, every query |
| Metric definition | Plain description, what it can be split by, also-called names, "Ask about it" | "How it's calculated" (SQL), lineage |
| Task details | Title, state, when, outcome, the next action | "Steps" list with capability identifiers |
| Watching a metric | Metric, by month/quarter, sensitivity (big moves only / notable / any unusual), how often | The σ threshold is shown as the sensitivity's help text |
| Saved analysis | Title, how often verified, Run for a period, Repeat | "How it works": plan, required checks, dependencies |
| Build › Skills | (unchanged; engineering area) | Code, tests, capabilities, evaluation runs |

## 6. Glossary: the words the interface uses

| Interface says | Means | Replaces on the main path |
|---|---|---|
| Approved measure / metric | A definition an admin approved (what revenue means here). Lyst answers only from these | governed metric |
| Verified | Every number was recomputed independently and every required check passed | (badge without meaning) |
| Saved analysis | A verified answer Lyst can rerun for any period | workflow (kept in Build until Part B) |
| Watch a metric / alert | Lyst checks a metric on a schedule and investigates unusual moves | monitor, σ threshold |
| Sensitivity | How unusual a move must be before Lyst alerts: big moves only (4σ), notable (3σ), any unusual move (2σ) | threshold sigma |
| Repeat / scheduled question | A question Lyst asks on its own every day, week or month | schedule |
| What Lyst noticed | Changes found by a watched metric, with priority High/Medium/Low | findings, priority 3.2 |
| Needs you | Approvals, questions Lyst needs answered, results that did not verify | (unchanged) |
| Depth | How much of the method an answer shows | view |

## 7. Part A: implemented now (low regret, reversible)

* **Get started** on Home for the first run: add data, ask a first question, watch a metric or repeat a report,
  connect a model (admins). Each step links to where it is done; the card disappears when complete or dismissed.
* **Business depth by default** for everyone; the chosen depth is still remembered for the session; executive
  audiences still open at Executive.
* **Act bar on answers**: Watch this metric, Repeat it (weekly or monthly), one Download menu, How this ran.
* **Trust line** under the Verified badge, plain workflow captions, table headers in words, no negative zero,
  "What it means" / "What to do" headings, "Show the method, checks and queries".
* **Work**: sensitivity choices instead of a σ slider, Watched metrics / Scheduled questions / What Lyst noticed,
  priority as High/Medium/Low, approvals described in words, task steps behind an expander.
* **Data and Intelligence**: code names and SQL moved into the detail dialog and behind "How it's calculated".
* **Build**: opens on Dashboards (the business artefact); Workflows and Skills keep their places.
* **Glossary** in the sidebar: "What these words mean".

## 8. Part B: decided by Rohn (2026-11-04) and implemented

**B1. Areas named by the job: Home · Data · Brain · Settings** (Work became Brain › Work in November 2026). Intelligence is now **Brain** (definitions,
documents, memory). Build is folded into **Data**, which holds what Lyst reads and what it makes from it:
Datasets (with their Sources), Dashboards, Saved analyses, SQL. The builder tools (skills, the capability list,
evaluation runs) moved to **Settings › Advanced**. Five areas, one navigation. Watched metrics and scheduled
questions stay in Work › Recurring, since they produce work that lands there.

**B2. Depths named by how much they show: Summary · Detail · Method**, with **Brief** as an action: a dialog with the
two-line brief, the trust line, one chart, what to do and the PDF to forward. A depth remembered under an old name
(Business, Analyst, Engineer) opens the same depth; an executive request opens on the brief.

**B3. A business pulse on Home.** `GET /v1/workspaces/{ws}/pulse` returns each approved metric's last complete month
against the one before (change in points for ratios, percent otherwise), whether that move is good for the business,
and a twelve-month history. It runs through the SQL guard as the reader, so column and row policies apply and a
metric the reader cannot query is left out. Home shows up to six tiles; "Why?" on a tile asks why it changed. Cached
per session for five minutes.

## 9. How we will know

Re-walk the journeys in `docs/PRODUCT_EXCELLENCE.md` and count clicks; target for the new jobs:

| Job | Before | Target |
|---|---|---|
| Get alerted when the metric in an answer moves | 6 (Work, Recurring, Watch a metric, pick metric, pick grain, Propose) and knowledge of σ | 2 (Watch this metric, Confirm) |
| Repeat an answered question weekly | 5 via Build › Workflows dialog | 2 (Repeat it, Weekly) |
| Read an answer at business depth as an admin | 1 extra click on every answer | 0 |
| A new person's first verified answer | unguided | guided by Get started, 1 click from a suggestion |
