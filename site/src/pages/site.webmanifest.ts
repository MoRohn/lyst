import type { APIRoute } from 'astro';
import { withBase } from '../lib/url';

export const GET: APIRoute = () =>
  new Response(
    JSON.stringify({
      name: 'Lyst',
      short_name: 'Lyst',
      description: 'The open-source AI Analyst.',
      start_url: withBase('/'),
      display: 'browser',
      background_color: '#F8F9F7',
      theme_color: '#151A21',
      icons: [
        { src: withBase('/icon-192.png'), sizes: '192x192', type: 'image/png' },
        { src: withBase('/icon-512.png'), sizes: '512x512', type: 'image/png' },
      ],
    }),
    { headers: { 'Content-Type': 'application/manifest+json' } },
  );
