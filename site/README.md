# Lyst website

The public product site for Lyst, the open-source AI Analyst (GNU AGPL v3.0). A single static page built with
[Astro](https://astro.build), deployed to GitHub Pages, with an optional registration service in
[`waitlist/`](waitlist/README.md).

- No server runtime, no cookies, no analytics, no third-party scripts or fonts. Inter and IBM Plex
  Mono are self-hosted (SIL OFL 1.1); icons are [Lucide](https://lucide.dev) (ISC), inlined at build time.
- About 3 KB of JavaScript (gzipped) for the menu, scroll reveals, the walkthrough, demo tabs and the form.
  Everything else is HTML and CSS rendered at build time.
- Every figure in the product previews is synthetic and lives in [`src/data/demos.ts`](src/data/demos.ts).
  Nothing is generated or fetched at runtime, and the page says so next to each preview.

Astro was chosen over plain HTML because the page has twelve sections that share tokens, icons, chart
helpers and copy; components keep that maintainable while still shipping static HTML.

## Commands

Run from `site/`. Node 22.23+ (see `.nvmrc`).

| Command | What it does |
| --- | --- |
| `npm install` | Install dependencies. |
| `npm run dev` | Dev server at http://localhost:4321. |
| `npm run build` | Type-check (`astro check`) and build to `dist/`. |
| `npm run preview` | Serve `dist/` like GitHub Pages (respects `PUBLIC_BASE_PATH`). |
| `npm test` | Build three variants and run the Playwright suite against them and the real waitlist service. Needs `uv`. |
| `npm run lighthouse` | Lighthouse, mobile and desktop, 3 runs each, against `dist/`. Fails below 95. |
| `npm run test:links` | Check anchors, internal files and external links in `dist/`. |
| `npm run test:secrets` | Scan source and build for anything that looks like a credential. |
| `npm run licenses` | List production dependency licenses; fail on anything not reviewed. |
| `npm run brand` | Regenerate the logo, favicon, app icons and social card from `scripts/build-brand.mjs`. |

## Configuration

Everything that changes between environments is a public build-time variable (see `.env.example`).
None of them may hold a secret: they are compiled into the HTML.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUBLIC_SITE_URL` | `http://localhost:4321` | Origin for canonical, Open Graph, sitemap and robots URLs. |
| `PUBLIC_BASE_PATH` | `/` | `/lyst` for `https://USER.github.io/lyst/`; `/` for a custom domain. |
| `PUBLIC_LAUNCH_MODE` | `stealth` | `stealth` or `public`. See below. |
| `PUBLIC_WAITLIST_ENDPOINT` | empty | Base URL of the waitlist service. Empty shows a truthful "registration is not open" state instead of a form. |
| `PUBLIC_CONTACT_EMAIL` | empty | Shown in the footer and as the design-partner fallback only when set. |
| `PUBLIC_GITHUB_URL` | empty | Shown in the footer and the open-source section only when set. |

Copy lives in [`src/data/content.ts`](src/data/content.ts) and stage labels in
[`src/config/site.ts`](src/config/site.ts), so wording and capability status (`dev`, `planned`,
`available`) can change without touching components.

### Stealth to public

`PUBLIC_LAUNCH_MODE=public` switches the badge, hero note, primary call to action (to "Get Lyst",
linking to `PUBLIC_GITHUB_URL`), the footer status and the capability and enterprise notes. No layout
changes. Update each capability's `status` in `content.ts` as features ship.

## Early-access registration

GitHub Pages cannot receive form posts. The form posts JSON to `PUBLIC_WAITLIST_ENDPOINT` +
`/v1/registrations`, served by the optional FastAPI + DuckDB service in [`waitlist/`](waitlist/README.md)
(validation, rate limiting, honeypot, origin allow-list, encrypted fields, consent records, removal and
retention). Nothing is stored in the browser.

To test a real submission end to end locally:

```bash
cd waitlist && LYST_WAITLIST_ENV=development uv run lyst-waitlist serve --port 8787
```

```bash
PUBLIC_WAITLIST_ENDPOINT=http://localhost:8787 npm run build && npm run preview
```

Submit at http://localhost:4321/#early-access. The success message only appears when the service
answers `200`; network errors, `5xx` and `429` each show a specific message that says nothing was stored.

## Deploying to GitHub Pages

The workflow is [`.github/workflows/site.yml`](../.github/workflows/site.yml) at the repository root.
On pull requests it runs the tests; on pushes to `main` that touch `site/` it also builds and deploys.

1. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions** (one time).
2. Optionally add repository **variables** (Settings → Secrets and variables → Actions → Variables):
   `LYST_LAUNCH_MODE`, `LYST_WAITLIST_ENDPOINT`, `LYST_CONTACT_EMAIL`, `LYST_GITHUB_URL`, and to
   override detection, `LYST_SITE_URL` and `LYST_BASE_PATH`.
3. Push to `main`, or run the workflow manually from the Actions tab.

`actions/configure-pages` reports the live origin and base path, so the default project-site URL
(`https://USER.github.io/lyst/`) and a custom domain both get correct asset paths and canonical URLs
without code changes.

**Custom domain.** Add it under Settings → Pages → Custom domain, create the DNS records GitHub lists
(a `CNAME` to `USER.github.io` for a subdomain), wait for the certificate, then tick **Enforce HTTPS**.
The next deploy picks up the new origin automatically. No `CNAME` file is needed with Actions deploys.

**Failures and rollback.** A failing test or build stops the deploy and the previous version stays
live. To roll back a bad release, revert the commit on `main` (the workflow redeploys), or re-run the
last good "Website" workflow run from the Actions tab.

## Verification (2026-11-04, local, macOS, Chrome headless)

- `npm run build`: 0 type errors, 2 pages, `index.html` 177 KB raw, 34 KB gzipped (CSS and SVG inlined); 2.9 KB of gzipped JavaScript.
- Playwright: 22 tests passing, covering metadata, headings, anchors, navigation, mobile menu, keyboard
  and focus, axe (WCAG 2.2 AA, no serious or critical issues), overflow at 320–1920 px, the walkthrough
  (buttons, keyboard, autoplay and pause, reduced motion), demo tabs, the form against the real service
  (success, duplicate, client and server validation, network failure, 503, 429, design partner), the
  closed public-mode build, and the `/lyst/` base-path build.
- Waitlist service: 23 pytest tests passing, including 80 concurrent writes through the single writer.
- Lighthouse 13.6 (median of 3 runs): mobile 100 / 100 / 100 / 100, LCP 0.8 s, CLS 0.049, TBT 0 ms;
  desktop 100 / 100 / 100 / 100, LCP 0.2 s, CLS 0, TBT 0 ms. Reports in `reports/lighthouse/`.
- Links, secrets and licenses checks pass. All runtime and build dependencies are permissive and
  compatible with AGPL-3.0 distribution. Two weak-copyleft build tools (libvips via sharp, LGPL-3.0;
  lightningcss, MPL-2.0) are used unmodified at build time and nothing from them ships.

## Known limitations

- The brand assets are traced by hand from the supplied logo (`brand/lyst-logo-original.png`); the
  wordmark is Inter ExtraBold converted to outlines. Adjust `scripts/build-brand.mjs` and run
  `npm run brand` to refine proportions.
- The GitHub link, contact address and waitlist endpoint are unset until those exist.
- The waitlist service must be hosted separately (one process, persistent encrypted disk). It sends no
  email; confirmations and removal requests are handled by an operator with the CLI.
