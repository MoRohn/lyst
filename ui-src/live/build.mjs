// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Rohn Springfield
// Builds src/lyst/ui/live_component/live.js from src/live.js and writes THIRD_PARTY_LICENSES.txt beside it: every
// package the bundle contains, its version, its license and the license text. Fails on a license Lyst may not ship.
// Run: npm ci && npm run build (in ui-src/live). The output is committed, so installing Lyst needs no Node.
import { build } from 'esbuild';
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const OUT = '../../src/lyst/ui/live_component';
const ALLOWED = new Set(['Apache-2.0', 'MIT', 'ISC', 'BSD-2-Clause', 'BSD-3-Clause', '0BSD']);
const VERSION_OF = JSON.parse(readFileSync('package.json', 'utf8')).dependencies;

const result = await build({
  entryPoints: ['src/live.js'], bundle: true, format: 'esm', minify: true, target: 'es2022', metafile: true,
  legalComments: 'none', outfile: join(OUT, 'live.js'),
  banner: { js: `// Lyst live answer (AGPL-3.0-only). Bundles the AI SDK ai@${VERSION_OF.ai} and its dependencies; ` +
    'their licenses are in THIRD_PARTY_LICENSES.txt beside this file. Source: ui-src/live.' },
});

const packages = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  const m = input.match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
  if (!m || packages.has(m[1])) continue;
  const dir = input.slice(0, input.indexOf(m[0]) + m[0].length);
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
  const file = readdirSync(dir).find((f) => /^licen[cs]e/i.test(f));
  packages.set(m[1], { version: pkg.version, license: pkg.license, text: file ? readFileSync(join(dir, file), 'utf8').trim() : null });
}
const bad = [...packages].filter(([, p]) => !ALLOWED.has(p.license));
if (bad.length) {
  console.error('licenses Lyst may not ship:', bad.map(([n, p]) => `${n}@${p.version} ${p.license}`).join(', '));
  process.exit(1);
}
const lines = ['Third-party software in live.js (bundled by ui-src/live/build.mjs).', ''];
for (const [name, p] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
  lines.push('='.repeat(78), `${name} ${p.version} (${p.license})`, '='.repeat(78), p.text ||
    `${p.license}: the package ships no license file; see https://spdx.org/licenses/${p.license}.html`, '');
}
writeFileSync(join(OUT, 'THIRD_PARTY_LICENSES.txt'), lines.join('\n'));
console.log(`live.js: ${packages.size} packages, ${(readFileSync(join(OUT, 'live.js')).length / 1024).toFixed(0)} KB`);
