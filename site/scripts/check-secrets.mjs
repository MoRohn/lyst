// Fails if anything that looks like a credential is in the source or the build.
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';

const roots = process.argv.slice(2).length ? process.argv.slice(2) : ['src', 'public', 'dist', 'scripts', 'waitlist/lyst_waitlist', '.env.example'];
const patterns = [
  ['private key', /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ['AWS access key', /\bAKIA[0-9A-Z]{16}\b/],
  ['GitHub token', /\bgh[pousr]_[A-Za-z0-9]{36,}\b/],
  ['Anthropic/OpenAI key', /\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}\b/],
  ['Slack token', /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/],
  ['Google API key', /\bAIza[0-9A-Za-z_-]{35}\b/],
  ['Stripe key', /\b[rs]k_live_[0-9a-zA-Z]{20,}\b/],
  ['assigned secret', /(?:SECRET|PASSWORD|API_KEY|HASH_KEY|ENCRYPTION_KEY)\s*[=:]\s*['"]?[A-Za-z0-9+/_=-]{16,}/],
];
const skip = /node_modules|\.venv|__pycache__|\.(png|woff2|ico|duckdb)$/;
const hits = [];

async function walk(p) {
  let entries;
  try {
    entries = await readdir(p, { withFileTypes: true });
  } catch {
    return scan(p);
  }
  for (const e of entries) {
    const full = join(p, e.name);
    if (skip.test(full)) continue;
    if (e.isDirectory()) await walk(full);
    else await scan(full);
  }
}

async function scan(file) {
  if (skip.test(file)) return;
  let text;
  try {
    text = await readFile(file, 'utf8');
  } catch {
    return;
  }
  for (const [name, re] of patterns) if (re.test(text)) hits.push(`${name}: ${file}`);
}

for (const r of roots) await walk(r);
if (hits.length) {
  console.error(hits.join('\n'));
  process.exit(1);
}
console.log(`no secrets found in ${roots.join(', ')}`);
