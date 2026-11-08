// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// The Lyst App renderer. Runs inside a sandboxed iframe (sandbox="allow-scripts", no same-origin; the page's CSP is
// default-src 'none'): it cannot reach the network, the page, its cookies or storage. It receives the app by
// postMessage from the host (apps.js) and draws it with textContent and SVG only: nothing it receives is ever parsed
// as HTML. Every value it shows came from Lyst's semantic layer; the only number it computes is a what-if projection,
// which it labels as one.
//
// Messages in:  {lyst: 'theme', vars, labels}  {lyst: 'reset'}  {lyst: 'title', title, subtitle}
//               {lyst: 'block', block, data} (again for the same id: new values)
// Messages out: {lyst: 'ready'}  {lyst: 'height', height}

const CSS = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 var(--font, system-ui, sans-serif); color: var(--text); background: transparent; }
.app { display: flex; flex-direction: column; gap: 12px; padding: 2px; }
.head h2 { margin: 0; font-size: 1.15em; font-weight: 600; }
.head p { margin: 2px 0 0; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.card { background: var(--raised); border: 1px solid var(--border); border-radius: var(--r); padding: 12px 14px;
  display: flex; flex-direction: column; gap: 6px; min-width: 0; animation: rise .32s var(--ease) both; }
.card.wide { grid-column: 1 / -1; }
.card h3 { margin: 0; font-size: .8125em; font-weight: 500; color: var(--muted); }
.kpi .v { font-size: 1.6em; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.chg { font-size: .8125em; font-variant-numeric: tabular-nums; }
.chg.up { color: var(--good); } .chg.down { color: var(--bad); } .chg.flat { color: var(--muted); }
.meta { font-size: .75em; color: var(--muted); }
svg { display: block; width: 100%; height: auto; overflow: visible; }
.axis { fill: var(--muted); font-size: 10px; }
.line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.area { fill: var(--accent); opacity: .08; }
.dot { fill: var(--accent); }
.bars { display: grid; gap: 6px; }
.bar { display: grid; grid-template-columns: minmax(70px, 30%) 1fr auto; gap: 8px; align-items: center; font-size: .875em; }
.bar .k { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar .t { height: 10px; border-radius: 5px; background: var(--surface); overflow: hidden; }
.bar .f { height: 100%; background: var(--accent); border-radius: 5px; transition: width .3s var(--ease); }
.bar .n { font-variant-numeric: tabular-nums; color: var(--muted); }
.bar.on .f { background: var(--accent-strong, var(--accent)); } .bar.dim { opacity: .45; }
table { border-collapse: collapse; width: 100%; font-size: .8125em; font-variant-numeric: tabular-nums; }
th, td { padding: 4px 8px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; }
.scroll { overflow-x: auto; }
select, input[type=range] { font: inherit; color: inherit; accent-color: var(--accent); }
select { padding: 6px 8px; border-radius: 8px; border: 1px solid var(--border); background: var(--raised); }
.proj { font-size: .75em; color: var(--warn); }
.note { color: var(--text); white-space: pre-wrap; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@keyframes rise { from { opacity: 0; transform: translateY(4px); } }
@media (prefers-reduced-motion: reduce) { .card { animation: none; } .bar .f { transition: none; } }
`;

const state = { title: null, subtitle: null, blocks: [], data: {}, filter: '', labels: {}, pct: {} };
const L = (k, d) => state.labels[k] || d;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = String(text);
  return e;
}
const SVG = 'http://www.w3.org/2000/svg';
function s(tag, attrs, text) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  if (text != null) e.textContent = String(text);
  return e;
}

function fmt(v, m, compact = true) {
  if (v == null || Number.isNaN(v)) return '–';
  if (m.format === 'percent') return `${(v * 100).toFixed(1)}%`;
  const opts = compact && Math.abs(v) >= 10000 ? { notation: 'compact', maximumFractionDigits: 2 } : { maximumFractionDigits: 2 };
  if (m.format === 'currency') return new Intl.NumberFormat(undefined, { style: 'currency', currency: m.currency || 'USD', ...opts }).format(v);
  return new Intl.NumberFormat(undefined, opts).format(v);
}
function change(cur, prior, m) {
  if (cur == null || prior == null) return null;
  const d = cur - prior;
  const text = m.format === 'percent' ? `${d >= 0 ? '+' : ''}${(d * 100).toFixed(1)} pts`
    : `${d >= 0 ? '+' : ''}${fmt(d, m)}${prior ? ` (${d >= 0 ? '+' : ''}${((d / Math.abs(prior)) * 100).toFixed(1)}%)` : ''}`;
  const good = m.higher_is_better !== false ? d > 0 : d < 0;
  const none = m.format === 'percent' ? Math.abs(d) < 0.0005 : Math.abs(d) < 0.005;  // shows as zero
  return { text, cls: none ? 'flat' : good ? 'up' : 'down' };
}
const period = (p) => String(p || '').slice(0, 10);

// what a block shows under the current filter: the per-value data when the filter applies to its metric
function narrowed(d) {
  if (!state.filter || !d.by || !(state.filter in (d.by.values || {}))) return null;
  return d.by.values[state.filter];
}

function card(b, d, wide) {
  const c = el('section', `card ${b.type}${wide ? ' wide' : ''}`);
  c.setAttribute('aria-label', b.title || d.metric?.title || b.type);
  c.append(el('h3', null, b.title || d.metric?.title || ''));
  return c;
}

function kpi(b, d) {
  const c = card(b, d);
  const latest = narrowed(d) || d.latest || {};
  c.append(el('div', 'v', fmt(latest.value, d.metric)));
  const ch = change(latest.value, latest.prior, d.metric);
  if (ch) c.append(el('div', `chg ${ch.cls}`, `${ch.text} ${L('vsPrior', 'vs prior period')}`));
  c.append(el('div', 'meta', `${period(latest.period)}${state.filter && narrowed(d) ? ` · ${state.filter}` : ''}`));
  return c;
}

function trend(b, d) {
  const c = card(b, d, true);
  const series = (narrowed(d) || d.series || []).filter((p) => p.value != null);
  if (series.length < 2) { c.append(el('div', 'meta', L('noData', 'Not enough data yet.'))); return c; }
  const W = 600, H = 160, P = { l: 56, r: 4, t: 10, b: 18 };
  const vs = series.map((p) => p.value);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => P.l + (i / (series.length - 1)) * (W - P.l - P.r);
  const y = (v) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
  const pts = series.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`);
  const g = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `${d.metric.title}: ${fmt(vs[0], d.metric)} in ${period(series[0].period)} to ${fmt(vs.at(-1), d.metric)} in ${period(series.at(-1).period)}` });
  g.append(s('path', { class: 'area', d: `M${pts[0]} L${pts.join(' L')} L${x(series.length - 1)},${H - P.b} L${x(0)},${H - P.b} Z` }));
  g.append(s('polyline', { class: 'line', points: pts.join(' ') }));
  g.append(s('circle', { class: 'dot', cx: x(series.length - 1), cy: y(vs.at(-1)), r: 3.5 }));
  g.append(s('text', { class: 'axis', x: 0, y: P.t + 4 }, fmt(hi, d.metric)));  // the scale shown, so a change is read at its size
  g.append(s('text', { class: 'axis', x: 0, y: H - P.b }, fmt(lo, d.metric)));
  g.append(s('text', { class: 'axis', x: P.l, y: H - 4 }, period(series[0].period)));
  g.append(s('text', { class: 'axis', x: W - P.r, y: H - 4, 'text-anchor': 'end' }, period(series.at(-1).period)));
  g.append(s('text', { class: 'axis', x: W - P.r, y: Math.max(10, y(vs.at(-1)) - 8), 'text-anchor': 'end' }, fmt(vs.at(-1), d.metric)));
  c.append(g);
  return c;
}

function breakdown(b, d) {
  const c = card(b, d, true);
  const rows = d.rows || [];
  const max = Math.max(0, ...rows.map((r) => Math.abs(r.value || 0))) || 1;
  const list = el('div', 'bars');
  for (const r of rows) {
    const row = el('div', `bar${state.filter ? (state.filter === r.key ? ' on' : ' dim') : ''}`);
    const t = el('div', 't'); const f = el('div', 'f');
    f.style.width = `${(Math.abs(r.value || 0) / max) * 100}%`;
    t.append(f);
    row.append(el('span', 'k', r.key), t, el('span', 'n', fmt(r.value, d.metric)));
    list.append(row);
  }
  c.append(list, el('div', 'meta', `${L('by', 'by')} ${d.dimension} · ${period(d.periods?.[0])}`));
  return c;
}

function table(b, d) {
  const c = card(b, d, true);
  const keys = [...new Set((d.rows || []).map((r) => r.key))].filter((k) => !state.filter || k === state.filter);
  const t = el('table');
  const head = el('tr');
  head.append(el('th', null, d.dimension));
  for (const p of d.periods || []) head.append(el('th', null, period(p)));
  t.append(head);
  for (const k of keys) {
    const tr = el('tr');
    tr.append(el('td', null, k));
    for (const p of d.periods || []) tr.append(el('td', null, fmt(d.rows.find((r) => r.key === k && r.period === p)?.value, d.metric, false)));
    t.append(tr);
  }
  const wrap = el('div', 'scroll'); wrap.append(t);
  c.append(wrap);
  return c;
}

function filter(b, d) {
  const c = card(b, { metric: null });
  c.querySelector('h3').textContent = b.title || `${L('filter', 'Filter')}: ${d.dimension}`;
  const sel = el('select');
  sel.setAttribute('aria-label', b.title || d.dimension);
  const all = el('option', null, L('all', 'All')); all.value = ''; sel.append(all);
  for (const v of d.values || []) { const o = el('option', null, v); o.value = v; sel.append(o); }
  sel.value = state.filter;
  sel.addEventListener('change', () => { state.filter = sel.value; draw(); });
  c.append(sel);
  return c;
}

function whatif(b, d) {
  const c = card(b, d);
  const base = (narrowed(d) || d.latest || {}).value;
  const out = el('div', 'v kpi');
  const label = el('div', 'chg flat');
  const range = el('input');
  Object.assign(range, { type: 'range', min: b.min_pct, max: b.max_pct, step: 1, value: state.pct[b.id] || 0 });
  range.setAttribute('aria-label', `${d.metric.title}: ${L('change', 'change')} %`);
  const show = () => {
    const pct = Number(range.value);
    state.pct[b.id] = pct;
    out.textContent = fmt(base == null ? null : base * (1 + pct / 100), d.metric);
    label.textContent = `${pct >= 0 ? '+' : ''}${pct}% ${L('onLatest', 'on the latest value')} (${fmt(base, d.metric)})`;
  };
  range.addEventListener('input', show);
  show();
  c.append(out, range, label, el('div', 'proj', L('projection', 'Projection, not data')));
  return c;
}

function note(b) {
  const c = el('section', 'card wide');
  if (b.title) c.append(el('h3', null, b.title));
  c.append(el('div', 'note', b.text));
  return c;
}

const DRAW = { kpi, trend, breakdown, table, filter, whatif, note };
let root;

function draw() {
  root.replaceChildren();
  const app = el('div', 'app');
  if (state.title) {
    const h = el('header', 'head');
    h.append(el('h2', null, state.title));
    if (state.subtitle) h.append(el('p', null, state.subtitle));
    app.append(h);
  }
  const grid = el('div', 'grid');
  for (const b of state.blocks) grid.append((DRAW[b.type] || note)(b, state.data[b.id] || {}));
  app.append(grid);
  root.append(app);
  report();
}

let lastHeight = 0;
function report() {
  const h = Math.ceil(document.documentElement.scrollHeight);
  if (h !== lastHeight) { lastHeight = h; parent.postMessage({ lyst: 'height', height: h }, '*'); }
}

window.addEventListener('message', (e) => {
  if (e.source !== parent) return;
  const m = e.data || {};
  if (m.lyst === 'theme') {
    for (const [k, v] of Object.entries(m.vars || {})) document.documentElement.style.setProperty(k, v);
    state.labels = m.labels || {};
    draw();
  } else if (m.lyst === 'reset') {
    Object.assign(state, { title: null, subtitle: null, blocks: [], data: {}, filter: '', pct: {} });
    draw();
  } else if (m.lyst === 'title') {
    state.title = m.title; state.subtitle = m.subtitle; draw();
  } else if (m.lyst === 'block' && m.block) {  // a new block, or new values for one already drawn
    if (!(m.block.id in state.data)) state.blocks.push(m.block);
    state.data[m.block.id] = m.data || {};
    draw();
  }
});

const style = document.createElement('style');
style.textContent = CSS;
document.head.append(style);
root = el('main');
document.body.append(root);
new ResizeObserver(report).observe(document.documentElement);
parent.postMessage({ lyst: 'ready' }, '*');
