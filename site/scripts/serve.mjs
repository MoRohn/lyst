// Minimal static server that behaves like GitHub Pages for tests and Lighthouse:
// serves a build directory under an optional base path, resolves /dir/ to index.html,
// returns 404.html for unknown paths, and gzips text responses.
// Usage: node scripts/serve.mjs <dir> <port> [basePath]
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';
import { gzipSync } from 'node:zlib';

const [dir = 'dist', port = '4321', base = '/'] = process.argv.slice(2);
const root = resolve(dir);
const prefix = ('/' + base.replace(/^\/+|\/+$/g, '') + '/').replace('//', '/');
const types = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json',
  '.webmanifest': 'application/manifest+json',
  '.xml': 'application/xml; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};

async function file(p) {
  try {
    const s = await stat(p);
    if (s.isDirectory()) return file(join(p, 'index.html'));
    return { body: await readFile(p), type: types[extname(p)] ?? 'application/octet-stream' };
  } catch {
    return null;
  }
}

createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://x');
  let path = decodeURIComponent(url.pathname);
  if (prefix !== '/' && path === prefix.slice(0, -1)) {
    res.writeHead(301, { Location: prefix });
    return res.end();
  }
  let found = null;
  if (path.startsWith(prefix)) {
    const rel = normalize(path.slice(prefix.length)).replace(/^(\.\.[/\\])+/, '');
    found = await file(join(root, rel));
  }
  const status = found ? 200 : 404;
  found ??= (await file(join(root, '404.html'))) ?? { body: Buffer.from('Not found'), type: 'text/plain' };
  const headers = { 'Content-Type': found.type, 'Cache-Control': 'max-age=600' };
  let body = found.body;
  if (/text|json|xml|svg|javascript/.test(found.type) && /gzip/.test(req.headers['accept-encoding'] ?? '')) {
    body = gzipSync(body);
    headers['Content-Encoding'] = 'gzip';
  }
  res.writeHead(status, headers);
  res.end(req.method === 'HEAD' ? undefined : body);
}).listen(Number(port), '127.0.0.1', () => console.log(`serving ${root} at http://localhost:${port}${prefix}`));
