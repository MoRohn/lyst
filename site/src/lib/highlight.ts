/** Minimal build-time syntax highlighting for the SQL and Python snippets in the demos. */

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const SQL_KW =
  /\b(SELECT|FROM|JOIN|USING|WHERE|AND|OR|GROUP BY ALL|GROUP BY|ORDER BY|DESC|ASC|AS|FILTER|CREATE OR REPLACE TABLE|INT|ON|LIMIT)\b/g;
const SQL_FN = /\b(SUM|COUNT|AVG|DISTINCT|date_trunc)\b/g;

function tokenize(src: string, rules: [RegExp, string][]): string {
  // Split on comments and strings first so keywords inside them are left alone.
  const parts = src.split(/(--[^\n]*|#[^\n]*|'[^']*'|"[^"]*")/g);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) {
        const cls = part.startsWith('--') || part.startsWith('#') ? 'c' : 's';
        return `<span class="${cls}">${esc(part)}</span>`;
      }
      let out = esc(part);
      for (const [re, cls] of rules) out = out.replace(re, `<span class="${cls}">$1</span>`);
      return out;
    })
    .join('');
}

export function sql(src: string): string {
  return tokenize(src, [
    [SQL_KW, 'k'],
    [SQL_FN, 'f'],
    [/\b(\d+(?:\.\d+)?)\b(?![^<]*>)/g, 'n'],
  ]);
}

export function python(src: string): string {
  return tokenize(src, [
    [/\b(import|from|def|return|for|in|if|else|True|False|None)\b/g, 'k'],
    [/\b([a-z_]+)(?=\()/g, 'f'],
    [/\b(\d+(?:\.\d+)?)\b(?![^<]*>)/g, 'n'],
  ]);
}
