// Validates every link in a build: in-page anchors must hit an id, site-relative
// URLs must exist in the build, and external URLs must answer (HEAD, then GET).
// Usage: node scripts/check-links.mjs [dir] [--offline]
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';

const dir = process.argv[2] && !process.argv[2].startsWith('--') ? process.argv[2] : 'dist';
const offline = process.argv.includes('--offline');
const html = await readFile(join(dir, 'index.html'), 'utf8');
const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]));
const refs = [...html.matchAll(/\s(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
const problems = [];
const external = new Set();

for (const ref of new Set(refs)) {
  if (ref.startsWith('#')) {
    if (ref !== '#' && !ids.has(ref.slice(1))) problems.push(`missing anchor ${ref}`);
  } else if (/^https?:/.test(ref)) {
    external.add(ref);
  } else if (ref.startsWith('mailto:')) {
    continue;
  } else if (ref.startsWith('/')) {
    // Strip the configured base so we can look the file up in the build.
    const base = (process.env.PUBLIC_BASE_PATH || '/').replace(/\/$/, '');
    const rel = ref.split('#')[0].replace(new RegExp('^' + base), '') || '/';
    const path = join(dir, rel.endsWith('/') ? rel + 'index.html' : rel);
    try {
      await stat(path);
    } catch {
      problems.push(`missing file ${ref}`);
    }
  }
}

if (!offline) {
  for (const url of external) {
    if (/localhost|127\.0\.0\.1/.test(url)) continue;
    try {
      let res = await fetch(url, { method: 'HEAD', redirect: 'follow' });
      if (res.status >= 400) res = await fetch(url, { redirect: 'follow' });
      if (res.status >= 400) problems.push(`${res.status} ${url}`);
    } catch (e) {
      problems.push(`unreachable ${url} (${e.cause?.code ?? e.message})`);
    }
  }
}

console.log(`checked ${new Set(refs).size} links (${external.size} external${offline ? ', not fetched' : ''}), ${ids.size} ids`);
if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log('all links ok');
