import type { APIRoute } from 'astro';
import { absolute } from '../lib/url';

// Single-page site: one canonical URL. Extend the list if pages are added.
const pages = ['/'];

export const GET: APIRoute = ({ site }) => {
  const urls = pages.map((p) => `  <url><loc>${absolute(p, site)}</loc></url>`).join('\n');
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
    { headers: { 'Content-Type': 'application/xml; charset=utf-8' } },
  );
};
