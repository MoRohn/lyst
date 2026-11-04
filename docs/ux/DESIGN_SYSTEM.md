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

## Building blocks

* `kit.header`, `kit.sections` (≤ 3–4 sections, kept in the URL as `?view=`), `kit.label`, `kit.empty`, `kit.chip`.
* `kit.row`: one list item. Rows with actions reserve a status slot even when they have no status, so actions line
  up down a list (before: a row with a chip pushed its Open button right). On phones the empty slot collapses.
* `kit.guarded`: every API action answers with a toast or an inline error, and drops the cached "waiting" count so
  the Work badge is never stale after the person acts.
* `components.chart`: brand accent for the measure, muted history, dotted forecast in a shaded interval, latest point
  marked; transparent background in both themes; `alt` text with range and last value for screen readers.
* `components.table`: words for headings, percent/compact number formats, `—` for missing values, fixed height past
  12 rows (the grid's own sort, search, column resize, copy and CSV export remain).
* Command palette (`palette.py`): Ctrl/⌘+K or the sidebar's **Search or go to** button. Ask Lyst, jump to any
  area/section (with the words people use: "approvals" finds Work › Needs you), recent questions with engine status,
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
