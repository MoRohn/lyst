// Lists the licenses of every production dependency (what ships or builds the site)
// and fails on anything outside a permissive allow-list.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const allowed = /^(MIT|ISC|Apache-2\.0|BSD-2-Clause|BSD-3-Clause|0BSD|OFL-1\.1|CC0-1\.0|BlueOak-1\.0\.0|MIT-0|Python-2\.0|CC-BY-4\.0|Unlicense)$/;
// Weak-copyleft packages reviewed and accepted: build-time tools only, used unmodified,
// nothing from them is shipped in dist/. Compatible with commercial use.
const reviewed = {
  '@img/sharp-libvips-': 'LGPL-3.0 libvips binary, dynamically loaded by sharp (Astro image service, build time)',
  lightningcss: 'MPL-2.0 CSS minifier used by Vite at build time',
};
const tree = JSON.parse(execFileSync('npm', ['ls', '--omit=dev', '--all', '--json', '--long'], { encoding: 'utf8', maxBuffer: 64e6 }));
const seen = new Map();
(function walk(deps = {}) {
  for (const [name, info] of Object.entries(deps)) {
    if (!info.path || seen.has(name + '@' + info.version)) continue;
    let license = 'UNKNOWN';
    try {
      const pkg = JSON.parse(readFileSync(join(info.path, 'package.json'), 'utf8'));
      license = typeof pkg.license === 'string' ? pkg.license : pkg.license?.type ?? (pkg.licenses?.[0]?.type ?? 'UNKNOWN');
    } catch {}
    seen.set(name + '@' + info.version, license);
    walk(info.dependencies);
  }
})(tree.dependencies);

const counts = {};
const bad = [];
for (const [pkg, lic] of seen) {
  counts[lic] = (counts[lic] ?? 0) + 1;
  const parts = lic.replace(/[()]/g, '').split(/\s+OR\s+|\s+AND\s+/);
  const ok = lic.includes(' OR ') ? parts.some((p) => allowed.test(p)) : parts.every((p) => allowed.test(p));
  const note = Object.entries(reviewed).find(([prefix]) => pkg.startsWith(prefix));
  if (!ok && note) console.log(`reviewed ${pkg}: ${lic} (${note[1]})`);
  else if (!ok) bad.push(`${pkg}: ${lic}`);
}
console.log(`${seen.size} production packages`, counts);
if (bad.length) {
  console.error('needs review:\n' + bad.join('\n'));
  process.exit(1);
}
console.log('licenses ok');
