// @ts-check
import { defineConfig } from 'astro/config';

// Every deployment-specific value comes from the environment so the same
// source builds for http://localhost, https://USER.github.io/REPO/ and a
// custom domain. See README "Configuration".
const site = (process.env.PUBLIC_SITE_URL || 'http://localhost:4321').replace(/\/+$/, '');
const rawBase = process.env.PUBLIC_BASE_PATH || '/';
const base = ('/' + rawBase.replace(/^\/+|\/+$/g, '') + '/').replace(/^\/\/$/, '/');

export default defineConfig({
  site,
  base,
  trailingSlash: 'ignore',
  build: { inlineStylesheets: 'always', assets: '_assets' },
  compressHTML: true,
  devToolbar: { enabled: false },
});
