// Runs Lighthouse (mobile and desktop presets, default simulated throttling) against a
// production build served by scripts/serve.mjs, writes reports/lighthouse/*.json|html and
// a summary, and fails if any category scores below 95.
// Usage: npm run build && npm run lighthouse   (CHROME_PATH may point at a Chrome binary)
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import lighthouse from 'lighthouse';
import desktopConfig from 'lighthouse/core/config/desktop-config.js';
import * as chromeLauncher from 'chrome-launcher';

const PORT = 4399;
const url = `http://localhost:${PORT}/`;
const server = spawn('node', ['scripts/serve.mjs', 'dist', String(PORT)], { stdio: 'ignore' });
await new Promise((r) => setTimeout(r, 600));

const chrome = await chromeLauncher.launch({ chromeFlags: ['--headless=new', '--no-sandbox'] });
const out = 'reports/lighthouse';
mkdirSync(out, { recursive: true });
const summary = {};
let failed = false;

try {
  for (const [name, config] of [
    ['mobile', undefined],
    ['desktop', desktopConfig],
  ]) {
    const scores = [];
    for (let run = 0; run < 3; run++) {
      const result = await lighthouse(url, { port: chrome.port, output: ['json', 'html'], logLevel: 'error' }, config);
      const lhr = result.lhr;
      scores.push({
        performance: Math.round(lhr.categories.performance.score * 100),
        accessibility: Math.round(lhr.categories.accessibility.score * 100),
        'best-practices': Math.round(lhr.categories['best-practices'].score * 100),
        seo: Math.round(lhr.categories.seo.score * 100),
        LCP_ms: Math.round(lhr.audits['largest-contentful-paint'].numericValue),
        CLS: Number(lhr.audits['cumulative-layout-shift'].numericValue.toFixed(3)),
        TBT_ms: Math.round(lhr.audits['total-blocking-time'].numericValue),
        FCP_ms: Math.round(lhr.audits['first-contentful-paint'].numericValue),
      });
      if (run === 0) {
        writeFileSync(`${out}/${name}.json`, result.report[0]);
        writeFileSync(`${out}/${name}.html`, result.report[1]);
      }
    }
    // Median run by performance score
    const median = [...scores].sort((a, b) => a.performance - b.performance)[1];
    summary[name] = { median, runs: scores };
    for (const k of ['performance', 'accessibility', 'best-practices', 'seo']) if (median[k] < 95) failed = true;
  }
} finally {
  await chrome.kill();
  server.kill();
}

summary.url = url;
summary.lighthouse = (await import('lighthouse/package.json', { with: { type: 'json' } })).default.version;
summary.date = new Date().toISOString();
writeFileSync(`${out}/summary.json`, JSON.stringify(summary, null, 2));
console.log(JSON.stringify({ mobile: summary.mobile.median, desktop: summary.desktop.median }, null, 2));
process.exit(failed ? 1 : 0);
