// Generates every Lyst brand asset from one geometric definition:
//   brand/*.svg              logo, mark and wordmark masters
//   src/data/brand.json      path data inlined by <Logo />
//   public/favicon.svg, *.png app icons, public/og-image.png social card
// Text is converted to outlines with opentype.js, so no asset depends on a font.
// Run: npm run brand
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import opentype from 'opentype.js';
import sharp from 'sharp';

const require = createRequire(import.meta.url);
const fontFile = (weight) =>
  require.resolve(`@fontsource/inter/files/inter-latin-${weight}-normal.woff`);
const load = (weight) => {
  const buf = readFileSync(fontFile(weight));
  return opentype.parse(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
};
const semibold = load(600);
const regular = load(400);

const C = {
  graphite: '#151A21',
  deep: '#10151B',
  offwhite: '#F8F9F7',
  slate: '#586471',
  teal: '#55B8A8',
  tealDark: '#257E74',
  tealDeep: '#1D665E',
  tealMid: '#3E9F92',
  border: '#DFE4E7',
};

// ---- Mark: a ribbon that runs down and folds 45° to the right, forming an L.
// Drawn on a 32 × 32 grid.
const mark = {
  viewBox: '0 0 32 32',
  stem: 'M6 3h8v17.5L6 28.5z',
  foot: 'M6 28.5 14 20.5h13v8z',
  seam: 'M6 28.5 14 20.5v3.2L9.2 28.5z',
};
const markDefs = (id) => `<defs>
  <linearGradient id="${id}s" x1="10" y1="3" x2="10" y2="28.5" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${C.tealDark}"/><stop offset="1" stop-color="${C.tealDeep}"/></linearGradient>
  <linearGradient id="${id}f" x1="6" y1="24.5" x2="27" y2="24.5" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${C.tealMid}"/><stop offset="1" stop-color="${C.teal}"/></linearGradient>
</defs>`;
const markShapes = (id) =>
  `<path d="${mark.stem}" fill="url(#${id}s)"/><path d="${mark.foot}" fill="url(#${id}f)"/><path d="${mark.seam}" fill="${C.tealDeep}" fill-opacity=".35"/>`;

// ---- Wordmark
function textPath(font, text, size, tracking = 0) {
  let x = 0;
  const parts = [];
  const glyphs = [...text].map((ch) => font.charToGlyph(ch));
  glyphs.forEach((g, i) => {
    parts.push(g.getPath(x, 0, size).toPathData(2));
    x += (g.advanceWidth / font.unitsPerEm) * size + tracking;
    if (i < glyphs.length - 1) x += (font.getKerningValue(g, glyphs[i + 1]) / font.unitsPerEm) * size;
  });
  const box = { y1: 0, y2: 0 };
  glyphs.forEach((g) => {
    const b = g.getPath(0, 0, size).getBoundingBox();
    box.y1 = Math.min(box.y1, b.y1);
    box.y2 = Math.max(box.y2, b.y2);
  });
  return { d: parts.join(''), width: x - tracking, box };
}

const WM_SIZE = 100;
const wm = textPath(semibold, 'Lyst', WM_SIZE, -2.5);
const ascent = (semibold.tables.os2.sCapHeight / semibold.unitsPerEm) * WM_SIZE; // cap height
const descent = wm.box.y2; // descender depth below baseline (y grows downward)

// Wordmark viewBox: from cap top to descender bottom.
const wmView = { x: 0, y: -ascent, w: Math.ceil(wm.width + 1), h: Math.ceil(ascent + descent + 1) };

// Logo lockup: mark scaled to cap height + descender region, gap, wordmark.
function lockup(textColor, id = 'lm') {
  const h = 32;
  const scale = (h / ascent) * 0.78; // wordmark cap height ≈ 78% of mark height
  const wmW = wmView.w * scale;
  const gap = 9;
  const baseline = 3 + ascent * scale + (28.5 - 3 - ascent * scale); // align baseline with foot bottom
  const width = Math.ceil(32 + gap + wmW);
  const height = Math.ceil(Math.max(32, baseline + descent * scale + 1));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Lyst">${markDefs(id)}${markShapes(id)}<path transform="translate(${32 + gap} ${baseline.toFixed(2)}) scale(${scale.toFixed(4)})" d="${wm.d}" fill="${textColor}"/></svg>`;
  return { svg, width, height, wmTransform: `translate(${32 + gap} ${baseline.toFixed(2)}) scale(${scale.toFixed(4)})` };
}

const markSvg = (id = 'mk') =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${mark.viewBox}" role="img" aria-label="Lyst">${markDefs(id)}${markShapes(id)}</svg>`;

const light = lockup(C.graphite);
const dark = lockup(C.offwhite);

mkdirSync('brand', { recursive: true });
writeFileSync('brand/lyst-mark.svg', markSvg());
writeFileSync('brand/lyst-logo.svg', light.svg);
writeFileSync('brand/lyst-logo-on-dark.svg', dark.svg);
writeFileSync(
  'brand/lyst-wordmark.svg',
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${wmView.x} ${wmView.y.toFixed(1)} ${wmView.w} ${wmView.h}" role="img" aria-label="Lyst"><path d="${wm.d}" fill="${C.graphite}"/></svg>`,
);
writeFileSync('public/favicon.svg', markSvg('fv'));

writeFileSync(
  'src/data/brand.json',
  JSON.stringify(
    {
      mark,
      colors: C,
      lockup: { width: light.width, height: light.height, wordmark: wm.d, transform: light.wmTransform },
    },
    null,
    1,
  ),
);

// ---- App icons (mark on rounded off-white tile)
async function icon(size, file, pad = 0.18, radius = 0.22) {
  const inner = size * (1 - pad * 2);
  const s = inner / 32;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><rect width="${size}" height="${size}" rx="${size * radius}" fill="${C.offwhite}"/>${markDefs('ic')}<g transform="translate(${(size - inner) / 2 - 0.5 * s} ${(size - inner) / 2 + 0.25 * s}) scale(${s})">${markShapes('ic')}</g></svg>`;
  await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toFile(file);
}
await icon(180, 'public/apple-touch-icon.png', 0.16, 0);
await icon(192, 'public/icon-192.png');
await icon(512, 'public/icon-512.png');
await sharp(Buffer.from(markSvg('p32').replace('<svg ', '<svg width="64" height="64" '))).resize(32, 32).png().toFile('public/favicon-32.png');

// ---- Social card 1200 × 630
{
  const W = 1200, H = 630;
  const t1 = textPath(semibold, 'Your Analyst. Multiplied.', 76, -2);
  const t2a = textPath(regular, 'The open-source AI Analyst built to turn business questions', 28);
  const t2b = textPath(regular, 'into verified insights and completed analytical work.', 28);
  const t3 = textPath(semibold, 'PYTHON  ·  DUCKDB  ·  OPEN SOURCE  ·  SELF-HOSTED', 17, 1.4);
  const s = 56 / 32;
  const logoScale = 1.55;
  // Decorative chart on the right edge, faint grid + bars (synthetic, illustrative only)
  const bars = [0.42, 0.55, 0.5, 0.66, 0.61, 0.78, 0.92]
    .map((v, i) => {
      const bh = v * 200;
      return `<rect x="${860 + i * 40}" y="${560 - bh}" width="26" height="${bh}" rx="3" fill="${i === 6 ? C.teal : '#26313B'}"/>`;
    })
    .join('');
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
<rect width="${W}" height="${H}" fill="${C.deep}"/>
<g stroke="#1E2731" stroke-width="1">${Array.from({ length: 13 }, (_, i) => `<path d="M${i * 100} 0V${H}"/>`).join('')}${Array.from({ length: 7 }, (_, i) => `<path d="M0 ${i * 100 + 15}H${W}"/>`).join('')}</g>
<rect x="0" y="0" width="${W}" height="6" fill="${C.teal}"/>
${bars}
<g transform="translate(88 86) scale(${logoScale})">${markDefs('og')}${markShapes('og')}<path transform="${dark.wmTransform}" d="${wm.d}" fill="${C.offwhite}"/></g>
<path transform="translate(88 330)" d="${t1.d}" fill="${C.offwhite}"/>
<path transform="translate(90 398)" d="${t2a.d}" fill="#AEB8C2"/>
<path transform="translate(90 440)" d="${t2b.d}" fill="#AEB8C2"/>
<path transform="translate(90 548)" d="${t3.d}" fill="${C.teal}"/>
</svg>`;
  await sharp(Buffer.from(svg)).png({ compressionLevel: 9, palette: false }).toFile('public/og-image.png');
  writeFileSync('brand/og-image.svg', svg);
}
console.log('brand assets written', { lockup: [light.width, light.height] });
