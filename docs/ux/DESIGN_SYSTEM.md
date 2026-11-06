# Lyst design system

The rules and building blocks every Lyst screen is made of, and the register of interface problems found by
rendering the real product. Code lives in `src/lyst/ui/tokens.py` (tokens), `src/lyst/ui/kit.py` (page building
blocks and the stylesheet), `src/lyst/ui/components.py` (analytical components) and `src/lyst/ui/palette.py`
(command palette).

## Tokens

One source for each decision:

| Decision | Where | How a page uses it |
| --- | --- | --- |
| Colours (accent, surfaces, text, borders, chart series, badge text) | `.streamlit/config.toml` `[theme.light]` / `[theme.dark]` | Streamlit widgets read it directly; `tokens.colors(mode)` derives semantic names (`text-muted`, `grid`, `focus`, `good`, `warn`, `bad`, `info`) |
| Type scale, weights, leading, tracking | `tokens.TYPE` | `var(--lyst-size-xl)` etc. |
| Spacing (4px grid) | `tokens.SPACE` | `var(--lyst-space-1..7)` |
| Layout widths | `tokens.LAYOUT` | `--lyst-content-max` (1120px reading column), `--lyst-canvas-max`, `--lyst-gutter-mobile` |
| Radius, motion, icon sizes | `tokens.RADIUS`, `MOTION`, `ICON` | `--lyst-radius-*`, `--lyst-motion-*` (0 ms under `prefers-reduced-motion`) |
| Chart styling | `tokens.plotly_layout(mode, fmt=)` | every Plotly chart in `components.chart()` |

`kit.style()` writes the token block for the active theme (`st.context.theme.type`) ahead of the stylesheet. The
stylesheet contains no raw colours (a test enforces it). Contrast is tested: every text and status token is at least
4.5:1 on both the background and the surface, in both themes (`tests/test_ui_tokens.py`).

Selectors target only `data-testid`, ARIA attributes (`aria-checked`), `st-key-<key>` classes and Prism token
classes, never Streamlit's generated `st-emotion-cache-*` classes.

## Lyst material

The visual language, inspired by the craft of native apps without imitating them:

* **Canvas and surfaces.** A faintly cool canvas (`#f6f7f9` light, `#0b0c0f` dark) with raised surfaces on it
  (`--lyst-raised`: white / graphite), a soft edge (`--lyst-edge`) and three short shadows (`--lyst-shadow-1..3`; dark
  mode uses a light inner edge instead of heavy shadow). Cards (`kit.card`), every bordered container and form, and
  expanders are raised. Streamlit gives bordered containers no attribute, so one small observer in `kit.style()`
  marks them `data-lyst-surface`; without JavaScript they keep their hairline.
* **Material.** The sidebar and toasts are translucent (`--lyst-material` + backdrop blur); dialogs are sheets with a
  20px radius, the deepest shadow and a rise-in.
* **Type.** The system UI face (SF Pro on Apple devices, Segoe UI Variable on Windows), a 2rem large title at
  -0.022em, tabular figures for data.
* **Controls.** Segmented controls are an inset track with a raised thumb; buttons press (scale .975) and secondary
  buttons sit raised; inputs glow with the accent on focus; the current page in the sidebar is a raised pill with an
  accent icon.
* **Motion.** Pages rise in over 320 ms, sheets over 320 ms, controls over 120 ms; all 0 ms under
  `prefers-reduced-motion`. Motion never stands in for progress: status comes from the engine.
* **Sidebar.** Search (⌘K) and workspace first, then **Recent**: the latest four questions with their real status
  icon (running, waiting for approval, failed), one click back to each.

## The pages in the new language

* **Home canvas.** An answer opens as a canvas: the answer on the left, its **evidence rail** beside it (checks passed,
  the data and version it came from, the expertise applied with each rule's outcome, assumptions, the method's size).
  **Focus** hides the rail. Home's tiles are `visuals.metric_tile` cards (value, a change coloured by whether it is
  good for that measure, a sparkline with alt text) instead of `st.metric`'s Vega chart.
* **Four areas: Home · Data · Brain · Settings.** Work is a section of Brain (Brain › Work: running now, Needs you,
  Activity, Recurring); the hidden `work` page forwards old `/work?view=…` links there.
* **Agentic layer (`lyst.ui.agent`).** One live status line (`agent.pulse`) says what Lyst is doing now; Home leads
  with it, what needs you, and answer-first cards (the answer, a preview from its own numbers, *Why, and what to do*
  folded underneath, **Open** for the full artifact). Data opens with Lyst's read on the data, each flagged dataset
  one click from its record. Brain opens on a band (`agent.hero`) of what Lyst knows, is doing and runs on its own,
  over a drawing made from those same counts (decorative, with the counts as its alt text).
  Brain › Work › Activity is the full history as a day-grouped list; the board view of the same work is the
  activity zone below. Task details show a progress meter and a step timeline (`visuals.steps`).
* **Activity zone (`lyst.ui.zone`).** The same Kanban board on every page, under the page's header: **Suggested ·
  Needs you · In progress · Done** (the last seven days), always in that order. A page opens on its own cards (Home:
  your questions and what Lyst suggests next; Data: datasets and sources; Brain: metrics, monitors, schedules,
  investigations; Settings: rules, skills, credentials) and one switch shows **Everywhere**, each card then tagged
  with its page. Cards are engine records (tasks, automatic activities, approvals, monitor findings, anticipations),
  built and tested in Python (`zone.build`); a running card shows the engine's own step count. Gestures: **click**
  opens a card where it lives; **drag** it to another column, and each column says what a drop does ("Drop to
  approve") while the ones it cannot go to stand back; **press and hold**, the ⋯ button or **M** opens its actions;
  on the keyboard, arrows move between cards, **Space** picks one up, arrows choose a column and Space drops it.
  Nothing runs from a gesture: anything that changes something opens a confirmation (the card, where it moves, what
  happens, how it is governed) and only Confirm calls the API, which keeps its own approvals and permissions. A
  viewer's cards only open. The board is a `st.components.v2` component (`zone_component/`): in the page, no iframe,
  no library, drawn with the page's tokens in both themes, refreshed every 8 seconds while something is in progress.
* **Settings.** A native two-pane screen (`kit.panes`): panes listed on the left, the chosen pane on the right,
  kept in `?view=`. Workspace settings sit in titled, raised groups (`kit.group`).

## Artifacts

Pages are workspaces; what Lyst makes are **artifacts** people find, open, refine and come back to
(`src/lyst/ui/artifacts.py`). Nothing about an artifact lives in Streamlit session state:

* **Model.** `catalog()` lists analyses (finished tasks and their results) and the asset registry (dashboards, saved
  analyses, PDF and Markdown briefings, slides, workbooks: ids, versions, lineage, validation and health). A chart
  inside an analysis is an artifact too, addressed `<task id>:<n>`.
* **Previews.** `thumbnail()` draws each tile from the artifact's own content: an analysis or chart from its numbers
  (line or bars), a file from its kind (dashboard layout, deck, page, grid, flow). SVG images, so they render in one pass.
* **Viewer.** The hidden `artifact` page (`?kind=&id=`, not a navigation area) opens any artifact with room to work:
  Back returns to the exact page and section it was opened from, Pin keeps it in the sidebar, Versions lists its
  versions, an Evidence column shows what it is built on, and **Refine it** sends a follow-up ("break that down by
  region", "compare it to last year", "make this a dashboard") that runs as a new analysis recording its parent; if
  the engine cannot apply a refinement it says which ones it can. `VIEWERS` maps each kind to its own viewer: a
  chart large with its table; a dashboard's own published series, interactive, with a period range; a saved
  analysis as its steps and a run for any period; a Markdown briefing as a document; a PDF page by page; slides as
  slides; a workbook sheet by sheet. Analyses open in the conversation on Home, where they can be continued.
* **Where they appear.** Home's **Your work** shelf (replacing the KPI tiles: Home is a workspace, not a dashboard),
  **Made in this answer** under each answer (each chart as a tile; one chart stays in the finding), Data › Dashboards
  as the studio gallery (search, filter by kind; the maintenance lists one step away), the ⌘K palette, and the
  sidebar's **Pinned**.
* **Pins.** Per person and workspace in the workspace database (`ui_pins`, migration 80; `GET/PUT/DELETE
  /v1/workspaces/{ws}/pins/{kind}/{id}`), so they survive reloads, browsers and sessions and never cross workspaces.

## Data and Brain as workspaces

Workspaces show objects and how they relate, not numbers: values live in artifacts and in each object's own view.

* **Data › Datasets** has **Cards** (shape, freshness, columns by role, grain, the metrics a dataset feeds; no metric
  sparklines) and **Map**: every dataset on a ring, linked by the columns they share, drawn from discovery's
  evidence (`GET relations`). Column profiles open from a dataset.
* **Brain › Definitions** shows knowledge cards (what a metric means, its formula as written, the dimensions it
  splits by, its synonyms; no live values). How definitions, data and terms connect is Brain › Graph's job (#115).
  A definition's trend and lineage open with it.
* `visuals.ring_graph` and `visuals.layered_graph` draw maps as SVG images whose alt text lists every connection.

### The Data workspace (after AcuArch's Data page)

Data follows AcuArch's data surface, in Lyst's tokens and Streamlit, and keeps every Lyst use case:

* **Header**: title and purpose; on the right the sources' status as a pill with a coloured dot (worst news first:
  a source that needs action, uploads to review, sources to check, all working) that opens Sources, and **Add data**.
* **Tabs**: Datasets · Sources · Dashboards · Saved analyses · SQL as icon pills, kept in `?view=` (palette links and
  `?dataset=` / `?connection=` deep links land on the right one). **Ask about the data** is docked at the end of the
  tab row, with three starting questions.
* **Datasets**: a **Library** rail (find anything; datasets with a warning icon when one needs attention, *Build a
  dataset*, approved metrics that open in Brain, dashboards that open in their viewer) beside a dotted **canvas**
  holding Cards, Map or **List** (every dataset as a row, and every relationship with its shared columns). At-a-glance
  pills sit above the canvas; a dataset opens in its dialog. On a phone the rail follows the canvas.
* **Sources, Dashboards, Saved analyses** open with an intro card: what the view is, its one action (Connect a
  source, New dashboard) and at-a-glance pills. Sources holds uploads waiting for review, proposed connections and
  connected sources with their health; empty states are dashed panels.
* **SQL**: the schema and recent queries in the same rail style; the editor and Run in one card; the result's row
  count and time as pills. The Python tab still joins through `WORKBENCH`.
* Styles are page-scoped in `app_pages/data.py` (`_CSS`), written only in `--lyst-*` tokens.

## Actions and progress (the autonomy layer, in place)

Autonomy is not a page. Every object offers what Lyst can do with it, where the object is (`src/lyst/ui/actions.py`):

* `actions.menu(kind, id, page=)` lists `GET /v1/workspaces/{ws}/objects/{kind}/{id}/actions` (#121) as **Ask Lyst**,
  **Do** and **Go to**, marks what needs approval, and runs through `POST .../actions/{id}`, which re-checks the
  offer server-side. The result is shown as what it is: the new task opens, an approval or schedule is announced as
  waiting, a draft as drafted, an artifact opens in the viewer. The page holds no agent logic, and on an engine
  without the API the menu simply does not appear.
* Menus live on answers (**More** beside Brief and Focus), metric definitions, Work's task details and the artifact
  viewer. Datasets get theirs on the Data page (owned by the Data redesign).
* **Progress is the engine's.** Work's list and board show each task's lifecycle state (Preparing, Investigating,
  Executing, Verifying, Awaiting Approval, Completed, Needs Attention) and what it is doing now; details add **How it
  progressed** from `GET /v1/tasks/{id}/lifecycle`. No percentages, no simulated motion.

## Building blocks

* `kit.header`, `kit.sections` (≤ 3–4 sections, kept in the URL as `?view=`), `kit.label`, `kit.empty`, `kit.chip`.
* `kit.row`: one list item. Rows with actions reserve a status slot even when they have no status, so actions line
  up down a list (before: a row with a chip pushed its Open button right). On phones the empty slot collapses.
* `kit.guarded`: every API action answers with a toast or an inline error, and drops the cached "waiting" count so
  the Home badge is never stale after the person acts.
* `components.chart`: brand accent for the measure, muted history, dotted forecast in a shaded interval, latest point
  marked; transparent background in both themes; `alt` text with range and last value for screen readers.
* `components.table`: words for headings, percent/compact number formats, `—` for missing values, fixed height past
  12 rows (the grid's own sort, search, column resize, copy and CSV export remain).
* Command palette (`palette.py`): Ctrl/⌘+K or the sidebar's **Search or go to** button. Ask Lyst, jump to any
  area/section (with the words people use: "approvals" finds Brain › Work › Needs you), recent questions with engine status,
  catalog results, matching Analyst specializations with their measured maturity label (catalog entries are not
  presented as proof of capability), and workspace switching. Everything is fetched for the current workspace when
  the palette opens.

## Visuals

`src/lyst/ui/visuals.py` draws small, data-dense visuals in one pass with the page (no chart library, no iframe):
`spark` (sparkline as an SVG image with `alt`, since `st.html` strips inline `<svg>`), `stacked` (proportion bar with
legend), `meter`, `bars` (labelled horizontal bars), `flow` (lineage boxes), `stats` (headline number strip,
`compact=True` inside cards) and `delta` (a change coloured by whether it is good for that measure, no arrow on zero).
Each has a text equivalent (`role="img"` + `aria-label`, or `alt`). `visuals.pulse()` reads each metric's latest value,
change and history from `GET pulse`, cached five minutes per workspace in the session.

Where they are used:

* **Data › Datasets**: a stat strip (datasets, rows, approved metrics, latest data, needing attention) and a card per
  dataset: classification, version and SQL name, health chip, rows/columns/data-through, columns by role (time,
  categories, numbers), its grain, the headline metric it feeds with value, change and sparkline, and Open / Ask.
* **Dataset › Columns**: a profile card per column: kind and type, completeness meter, distinct count, top values as
  bars for a category, lowest/median/highest for a number, the span for a date, and any issue.
* **Data › SQL**: a schema navigator (every dataset's columns and types, "Query it" writes a starting query), recent
  queries as one-line links (deduplicated) that load into the editor, results kept across reruns (they used to vanish
  on any click), a row count with "edited since it ran", and a Chart tab with a first chart (a date gives a line, a
  category a sorted bar). `WORKBENCH` in `data.py` is the slot for a Python tab beside SQL.
* **Brain › Definitions**: a card per approved metric with its latest value, change, twelve-period sparkline, meaning,
  source dataset and splits. The definition dialog adds the trend and a lineage flow (source → dataset → approved
  metric). `SECTIONS` in `brain.py` is the slot for a Specializations section.
* **Brain › Documents / Memory**: a stat strip (documents, citable passages, latest added) and a bar of what Lyst knows
  by trust (approved, validated by the team, observed).

## Checking a change

```bash
LYST_HOME=<scratch> LYST_KEYRING=0 LYST_MASTER_KEY=<random hex> LYST_PORT=8797 LYST_UI_PORT=8527 uv run lyst start --demo --no-browser
BASE=http://localhost:8527 VP=desktop:1440x900,mobile:390x844 THEMES=light,dark node scripts/ui/check.js var/ui-check
```

`scripts/ui/check.js` renders every area and section, saves a screenshot per page × viewport × theme, records render
time, horizontal overflow, Streamlit exceptions, console errors and axe-core WCAG 2.2 AA violations in
`report.json`, and exits non-zero on anything not allowed. It needs Node with `playwright` and
`@axe-core/playwright` on `NODE_PATH`; it is not part of `pytest` because the test machine has no browser.

## Improvement register

Found by rendering the demo (13 surfaces, light and dark, 1440, 820 and 390 px) on 2026-11-04. Status: Fixed (in this
change), Open (owner noted).

| # | Problem (evidence) | Root cause | Status |
| --- | --- | --- | --- |
| R1 | SQL view: 38 axe colour-contrast failures in recent queries; long queries cut off behind a horizontal scroll that keyboard users cannot reach (axe `scrollable-region-focusable`) | Streamlit's default Prism colours; `white-space: pre` | Fixed: token syntax colours, wrapped code |
| R2 | Green "Verified"/"Done" and red "to fix" badges at 4.3:1 on light | Streamlit's default badge text colours | Fixed: `*TextColor` theme options |
| R3 | Selected section text 3.7:1 on dark | accent used as text | Fixed: link colour for the selected section |
| R4 | Charts in Plotly's default blue, not the product's colours, white plot area | no chart theme | Fixed: `tokens.plotly_layout`, `theme=None` |
| R5 | Charts had no text alternative | no `alt` | Fixed for answer charts; Home sparklines still lack it (axe `svg-img-alt` ×6) | 
| R6 | List actions misaligned when one row has a status chip (Data › Datasets) | column widths differ per row | Fixed |
| R7 | No keyboard route anywhere; search only in the sidebar, sections only by clicking | no palette | Fixed: ⌘K palette |
| R8 | Five "waiting" API calls on every rerun of every widget, on every page (9 shell calls per page view) | count recomputed each run | Fixed: 20 s per-workspace cache, dropped on any action |
| R9 | No visible keyboard focus ring on tertiary buttons and sections | Streamlit default | Fixed: one `:focus-visible` ring |
| R10 | A by-region answer shows no chart in Summary; the breakdown table appears only at Detail | `answer()` renders tables only at Detail | Fixed: `breakdown_chart` draws the split as a bar in Summary |
| R11 | Answer action area: six differently sized buttons, four pills, two more buttons, next steps, follow-ups and a composer | grown feature by feature | Open (shared answer layout; coordinate with the UX thread) |
| R12 | Data and Brain are text lists: no dataset previews, freshness or health visuals, no definition/lineage visuals | list-first kit | Fixed: see Visuals |
| R17 | Data › SQL lost its result on any rerun (opening an expander, switching a tab) | result drawn only inside `if st.button` | Fixed: result kept in session state per workspace |
| R18 | Data › SQL recent queries were full code blocks (8 × ~100 px) with no way to reuse one | rendering choice | Fixed: one-line links that load the query |
| R13 | Home tile "0.0 pts" shows an up arrow | sign of zero | Open (Home tiles) |
| R14 | Sidebar stacks five full-width controls | each feature added its own | Partly: search moved into the palette trigger at the top |
| R15 | Console shows a 404 for a static resource on every page | pre-existing | Open |
| R16 | Palette input is not focused on open (one click needed) | Streamlit dialogs do not autofocus | Open |
