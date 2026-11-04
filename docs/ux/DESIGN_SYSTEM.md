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
| R10 | A by-region answer shows no chart in Summary; the breakdown table appears only at Detail | `answer()` renders tables only at Detail | Open (next change: breakdown bar in Summary) |
| R11 | Answer action area: six differently sized buttons, four pills, two more buttons, next steps, follow-ups and a composer | grown feature by feature | Open (shared answer layout; coordinate with the UX thread) |
| R12 | Data and Brain are text lists: no dataset previews, freshness or health visuals, no definition/lineage visuals | list-first kit | Open (visual overhaul of Data and Brain, next) |
| R13 | Home tile "0.0 pts" shows an up arrow | sign of zero | Open (Home tiles) |
| R14 | Sidebar stacks five full-width controls | each feature added its own | Partly: search moved into the palette trigger at the top |
| R15 | Console shows a 404 for a static resource on every page | pre-existing | Open |
| R16 | Palette input is not focused on open (one click needed) | Streamlit dialogs do not autofocus | Open |
