// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Usage: BASE=http://localhost:8501 VP=desktop:1440x900,mobile:390x844 THEMES=light,dark node scripts/ui/check.js <out-dir>
// Needs node with playwright and @axe-core/playwright on NODE_PATH. Writes one PNG per page x viewport x theme plus
// report.json (render ms, horizontal overflow, Streamlit exceptions, console errors, axe WCAG 2.2 AA violations).
// Exits 1 when any page raised an exception, overflowed horizontally or has an axe violation not in ALLOW.
// Render every Lyst surface, screenshot it, time it, collect console errors and axe violations.
const { chromium } = require('playwright');
const AxeBuilder = require('@axe-core/playwright').default;
const fs = require('fs');
const BASE = process.env.BASE || 'http://localhost:8501';
const OUT = process.argv[2] || 'var/ui-check';
const PAGES = [
  ['home', '/'], ['work-needs', '/work?view=needs+you'], ['work-activity', '/work?view=activity'],
  ['work-recurring', '/work?view=recurring'], ['data-datasets', '/data?view=datasets'],
  ['data-dashboards', '/data?view=dashboards'], ['data-saved', '/data?view=saved+analyses'], ['data-sql', '/data?view=sql'],
  ['brain-defs', '/brain?view=definitions'], ['brain-docs', '/brain?view=documents'], ['brain-memory', '/brain?view=memory'],
  ['settings', '/settings'], ['artifact-dashboard', '/artifact?kind=dashboard&id=' + (process.env.ART_DASH || 'none')],
];
const VIEWPORTS = (process.env.VP || 'desktop:1440x900').split(',').map(s => { const [n, d] = s.split(':'); const [w, h] = d.split('x').map(Number); return { n, w, h }; });
const THEMES = (process.env.THEMES || 'light').split(',');
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const report = [];
  for (const vp of VIEWPORTS) for (const theme of THEMES) {
    const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, colorScheme: theme, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    for (const [name, path] of PAGES) {
      const errors = [];
      page.removeAllListeners('console');
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
      const t0 = Date.now();
      await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
      // Streamlit: wait until it stops running (no status widget "Running") and main has content
      await page.waitForSelector('[data-testid="stMain"] h1, [data-testid="stMain"] [data-testid="stMarkdown"]', { timeout: 30000 }).catch(() => {});
      await page.waitForFunction(() => !document.querySelector('[data-testid="stStatusWidget"]'), null, { timeout: 30000 }).catch(() => {});
      await page.waitForTimeout(400);
      const ms = Date.now() - t0;
      const dims = await page.evaluate(() => {
        const m = document.querySelector('[data-testid="stMain"]');
        const b = document.querySelector('.block-container') || m;
        return { scrollH: m ? m.scrollHeight : 0, overflowX: document.documentElement.scrollWidth > window.innerWidth || (b && b.scrollWidth > b.clientWidth + 2),
                 exceptions: [...document.querySelectorAll('[data-testid="stException"]')].map(e => e.innerText.slice(0, 300)) };
      });
      await page.setViewportSize({ width: vp.w, height: Math.min(Math.max(vp.h, dims.scrollH + 40), 6000) });
      await page.waitForTimeout(300);
      const file = `${name}-${vp.n}-${theme}.png`;
      await page.screenshot({ path: `${OUT}/${file}` });
      await page.setViewportSize({ width: vp.w, height: vp.h });
      let axe = [];
      if (process.env.AXE !== '0' && theme === THEMES[0] && vp === VIEWPORTS[0]) {
        try {
          const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
          axe = r.violations.map(v => ({ id: v.id, impact: v.impact, n: v.nodes.length, sample: v.nodes.slice(0, 2).map(x => x.target.join(' ')) }));
        } catch (e) { axe = [{ id: 'axe-error', sample: [String(e).slice(0, 120)] }]; }
      }
      report.push({ name, vp: vp.n, theme, ms, ...dims, errors, axe, file });
      console.log(name, vp.n, theme, ms + 'ms', dims.overflowX ? 'OVERFLOW-X' : '', dims.exceptions.length ? 'EXC' : '', 'axe:' + axe.map(a => a.id + '×' + a.n).join(','));
    }
    await ctx.close();
  }
  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 1));
  await browser.close();
  // Known, recorded exceptions (docs/ux/DESIGN_SYSTEM.md, register): Home's metric sparklines have no SVG alt yet.
  const ALLOW = new Set((process.env.ALLOW || 'svg-img-alt').split(',').filter(Boolean));
  const bad = report.filter(r => r.overflowX || r.exceptions.length || r.axe.some(a => !ALLOW.has(a.id)));
  if (bad.length) { console.error('FAILED:', bad.map(r => `${r.name}/${r.vp}/${r.theme}`).join(' ')); process.exit(1); }
})();
