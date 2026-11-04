import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const CLOSED = 'http://localhost:4330/';
const SUB = 'http://localhost:4331/lyst/';

function trackErrors(page: Page) {
  const errors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('requestfailed', (r) => errors.push(`failed: ${r.url()}`));
  page.on('response', (r) => r.status() >= 400 && errors.push(`${r.status()}: ${r.url()}`));
  return errors;
}

test.describe('page', () => {
  test('loads cleanly with complete metadata', async ({ page }) => {
    const errors = trackErrors(page);
    await page.goto('/');
    await expect(page).toHaveTitle('LYST — Your Analyst. Multiplied. | Open-Source AI Analyst');
    await expect(page.locator('h1')).toHaveText('Your Analyst. Multiplied.');
    await expect(page.locator('h1')).toHaveCount(1);
    const meta = async (sel: string) => page.locator(sel).getAttribute('content');
    expect(await meta('meta[name="description"]')).toContain('open-source autonomous AI Analyst');
    expect(await page.locator('link[rel="canonical"]').getAttribute('href')).toBe('http://localhost:4329/');
    expect(await meta('meta[property="og:image"]')).toBe('http://localhost:4329/og-image.png');
    expect(await meta('meta[name="twitter:card"]')).toBe('summary_large_image');
    const ld = JSON.parse((await page.locator('script[type="application/ld+json"]').textContent())!);
    expect(ld['@graph'].map((n: { '@type': string }) => n['@type'])).toEqual(['WebSite', 'SoftwareApplication']);
    for (const path of ['/og-image.png', '/favicon.svg', '/robots.txt', '/sitemap.xml', '/site.webmanifest', '/apple-touch-icon.png']) {
      expect((await page.request.get(path)).status(), path).toBe(200);
    }
    const robots = await (await page.request.get('/robots.txt')).text();
    expect(robots).toContain('Sitemap: http://localhost:4329/sitemap.xml');
    expect(await (await page.request.get('/sitemap.xml')).text()).toContain('<loc>http://localhost:4329/</loc>');
    expect(errors).toEqual([]);
  });

  test('heading hierarchy never skips a level', async ({ page }) => {
    await page.goto('/');
    const levels = await page.$$eval('h1, h2, h3, h4', (hs) => hs.map((h) => Number(h.tagName[1])));
    for (let i = 1; i < levels.length; i++) expect(levels[i] - levels[i - 1]).toBeLessThanOrEqual(1);
  });

  test('every in-page link points at real content', async ({ page }) => {
    await page.goto('/');
    const hashes = await page.$$eval('a[href^="#"]', (as) => [...new Set(as.map((a) => a.getAttribute('href')!))]);
    expect(hashes.length).toBeGreaterThan(5);
    for (const h of hashes) expect(await page.locator(h).count(), h).toBe(1);
  });

  test('primary navigation scrolls to each section', async ({ page }) => {
    await page.goto('/');
    for (const [label, id] of [
      ['Product', 'product'],
      ['How It Works', 'how-it-works'],
      ['Capabilities', 'capabilities'],
      ['Open Source', 'open-source'],
      ['Early Access', 'early-access'],
    ]) {
      await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: label, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`#${id}$`));
      await expect(page.locator(`#${id}`)).toBeInViewport();
    }
  });

  test('no serious accessibility violations (axe)', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(() => document.querySelectorAll('[data-reveal]').forEach((e) => e.classList.add('is-visible')));
    await page.waitForTimeout(1500); // let reveal transitions finish so contrast is measured at rest
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
    const serious = results.violations.filter((v) => ['serious', 'critical'].includes(v.impact ?? ''));
    expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target).join(' | ')}`)).toEqual([]);
  });

  test('no horizontal overflow at common widths', async ({ page }) => {
    for (const width of [320, 375, 768, 1024, 1280, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `width ${width}`).toBeLessThanOrEqual(0);
    }
  });

  test('keyboard: skip link and visible focus', async ({ page }) => {
    await page.goto('/');
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: 'Skip to content' });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#main$/);
    await page.keyboard.press('Tab');
    const outline = await page.evaluate(() => getComputedStyle(document.activeElement!).outlineStyle);
    expect(outline).not.toBe('none');
  });
});

test.describe('interactive preview', () => {
  test.use({ reducedMotion: 'reduce' });

  test('steps forward, back, by keyboard and replays', async ({ page }) => {
    await page.goto('/');
    const preview = page.locator('[data-preview]');
    await expect(preview.locator('#pv-tab-discover')).toHaveAttribute('aria-selected', 'true');
    await expect(preview.locator('#pv-panel-discover')).toBeVisible();

    await preview.getByRole('button', { name: 'Next stage' }).click();
    await expect(preview.locator('#pv-panel-query')).toBeVisible();
    await expect(preview.locator('#pv-panel-discover')).toBeHidden();
    await expect(preview.getByText('Stage 2 of 6')).toBeVisible();

    await preview.getByRole('button', { name: 'Previous stage' }).click();
    await expect(preview.locator('#pv-panel-discover')).toBeVisible();

    await preview.locator('#pv-tab-discover').focus();
    await page.keyboard.press('End');
    await expect(preview.locator('#pv-panel-evidence')).toBeVisible();
    await expect(page.locator(':focus')).toHaveAttribute('id', 'pv-tab-evidence');
    await page.keyboard.press('ArrowUp');
    await expect(preview.locator('#pv-panel-findings')).toBeVisible();
    await expect(preview.getByRole('img', { name: /Operating expense bridge/ })).toBeVisible();

    await preview.locator('[data-next]').click();
    await expect(preview.getByRole('button', { name: 'Replay walkthrough from the start' })).toBeVisible();
    await preview.getByRole('button', { name: 'Replay walkthrough from the start' }).click();
    await expect(preview.locator('#pv-panel-discover')).toBeVisible();
  });

  test('does not auto-advance when reduced motion is requested', async ({ page }) => {
    await page.goto('/');
    await page.waitForTimeout(4000);
    await expect(page.locator('#pv-panel-discover')).toBeVisible();
  });

  test('is labelled as illustrative sample data', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('[data-preview]').getByText('Illustrative preview · sample data')).toBeVisible();
    await expect(page.getByText(/live AI inference/)).toBeVisible();
  });
});

test.describe('autoplay', () => {
  test.use({ reducedMotion: 'no-preference' });
  test('auto-advances when visible and pauses on request', async ({ page }) => {
    await page.goto('/');
    await page.locator('[data-preview]').scrollIntoViewIfNeeded();
    await expect(page.locator('#pv-panel-query')).toBeVisible({ timeout: 6000 });
    await page.getByRole('button', { name: 'Pause walkthrough' }).click();
    const panel = await page.locator('[data-stage-panel].is-active').getAttribute('id');
    await page.waitForTimeout(3600);
    await expect(page.locator('[data-stage-panel].is-active')).toHaveAttribute('id', panel!);
  });
});

test.describe('demonstrations', () => {
  test('tabs switch by click and arrow keys', async ({ page }) => {
    await page.goto('/#demos');
    const list = page.getByRole('tablist', { name: 'Demonstrations' });
    await expect(page.locator('#uc-panel-finance')).toBeVisible();
    await list.getByRole('tab', { name: 'Operational Intelligence' }).click();
    await expect(page.locator('#uc-panel-operations')).toBeVisible();
    await expect(page.locator('#uc-panel-finance')).toBeHidden();
    await page.keyboard.press('ArrowRight');
    await expect(page.locator('#uc-panel-engineering')).toBeVisible();
    await expect(list.getByRole('tab', { name: 'Analytical Engineering' })).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect(page.locator('#uc-panel-finance')).toBeVisible();
  });
});

test.describe('early access (configured backend)', () => {
  const unique = () => `qa+${Date.now()}${Math.floor(Math.random() * 1e6)}@example.com`;

  test('validates empty and invalid input without sending', async ({ page }) => {
    let sent = 0;
    page.on('request', (r) => r.url().includes('/v1/registrations') && sent++);
    await page.goto('/#early-access');
    const form = page.locator('#early-access form[data-waitlist]').first();
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByText('Enter your work email address.')).toBeVisible();
    await expect(form.getByText('Please confirm you agree to be contacted.')).toBeVisible();
    await expect(form.getByLabel(/Work email/)).toBeFocused();
    await expect(form.getByLabel(/Work email/)).toHaveAttribute('aria-invalid', 'true');
    await form.getByLabel(/Work email/).fill('not-an-email');
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByText(/Enter a valid email address/)).toBeVisible();
    expect(sent).toBe(0);
  });

  test('registers against the real service, then handles a duplicate', async ({ page, browser }) => {
    const email = unique();
    for (const p of [page, await browser.newPage()]) {
      await p.goto('http://localhost:4329/#early-access');
      const form = p.locator('#early-access form[data-waitlist]').first();
      await form.getByLabel(/Work email/).fill(email);
      await form.getByLabel(/Role/).selectOption('Analytics engineer');
      await form.getByLabel(/Organization/).fill('Example Co');
      await form.getByLabel(/Most interested in/).selectOption('Data pipelines');
      await form.getByLabel(/I agree that Lyst may email me/).check();
      const [res] = await Promise.all([
        p.waitForResponse((r) => r.url().endsWith('/v1/registrations') && r.request().method() === 'POST'),
        form.getByRole('button', { name: 'Join Early Access' }).click(),
      ]);
      expect(res.status()).toBe(200);
      await expect(form.getByRole('status')).toHaveText(/You’re on the early-access list/);
      await expect(form.getByLabel(/Work email/)).toBeDisabled();
    }
  });

  test('server-side validation errors are shown on the field', async ({ page }) => {
    await page.goto('/#early-access');
    const form = page.locator('#early-access form[data-waitlist]').first();
    // Passes the browser check but fails the server's stricter rule (single-label TLD).
    await form.getByLabel(/Work email/).fill('person@example.c0');
    await form.getByLabel(/I agree that Lyst may email me/).check();
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByRole('status')).toHaveText(/need attention/);
    await expect(form.getByText(/Enter a valid email address/)).toBeVisible();
  });

  test('network failure is reported truthfully', async ({ page }) => {
    await page.route('**/v1/registrations', (r) => r.abort('connectionrefused'));
    await page.goto('/#early-access');
    const form = page.locator('#early-access form[data-waitlist]').first();
    await form.getByLabel(/Work email/).fill(unique());
    await form.getByLabel(/I agree that Lyst may email me/).check();
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByRole('status')).toHaveText(/couldn’t reach the registration service. Nothing was stored/);
    await expect(form.getByLabel(/Work email/)).toBeEnabled();
  });

  test('server failure and rate limit are reported truthfully', async ({ page }) => {
    let status = 503;
    await page.route('**/v1/registrations', (r) => r.fulfill({ status, contentType: 'application/json', body: '{}' }));
    await page.goto('/#early-access');
    const form = page.locator('#early-access form[data-waitlist]').first();
    await form.getByLabel(/Work email/).fill(unique());
    await form.getByLabel(/I agree that Lyst may email me/).check();
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByRole('status')).toHaveText(/couldn’t save your registration/);
    status = 429;
    await form.getByRole('button', { name: 'Join Early Access' }).click();
    await expect(form.getByRole('status')).toHaveText(/Too many attempts/);
  });

  test('design partner application', async ({ page }) => {
    await page.goto('/#design-partners');
    await page.getByText('Apply as a design partner').click();
    const form = page.locator('[data-partner] form');
    await form.getByRole('button', { name: 'Send application' }).click();
    await expect(form.getByText('Enter your organization.')).toBeVisible();
    await form.getByLabel(/Work email/).fill(unique());
    await form.getByLabel(/Organization/).fill('Example Co');
    await form.getByLabel(/Analytics team size/).selectOption('6–20');
    await form.getByLabel(/What would you want Lyst to take on/).fill('Month-end variance commentary');
    await form.getByLabel(/design partner program/).check();
    await form.getByRole('button', { name: 'Send application' }).click();
    await expect(form.getByRole('status')).toHaveText(/design partner application was received/);
  });
});

test.describe('unconfigured, public-mode build', () => {
  test('shows a truthful closed state and public labels', async ({ page }) => {
    const errors = trackErrors(page);
    await page.goto(CLOSED);
    await expect(page.locator('form[data-waitlist]')).toHaveCount(0);
    await expect(page.getByText('Early-access registration is not open on this site yet.')).toBeVisible();
    await expect(page.locator('.hero .badge')).toContainText('Open-source release');
    await expect(page.getByRole('link', { name: 'GitHub repository' })).toHaveAttribute('href', 'https://github.com/MoRohn/lyst');
    await expect(page.locator('.hero').getByRole('link', { name: /Get Lyst/ })).toHaveAttribute('href', 'https://github.com/MoRohn/lyst');
    expect(errors).toEqual([]);
  });
});

test.describe('project-site base path (/lyst/)', () => {
  test('all assets resolve and metadata uses the public URL', async ({ page }) => {
    const errors = trackErrors(page);
    await page.goto(SUB);
    await expect(page.locator('h1')).toBeVisible();
    expect(await page.locator('link[rel="canonical"]').getAttribute('href')).toBe('https://morohn.github.io/lyst/');
    expect(await page.locator('meta[property="og:image"]').getAttribute('content')).toBe('https://morohn.github.io/lyst/og-image.png');
    expect(await page.locator('link[rel="icon"][type="image/svg+xml"]').getAttribute('href')).toBe('/lyst/favicon.svg');
    const fontOk = await page.evaluate(async () => {
      await document.fonts.ready;
      return document.fonts.check('16px Inter');
    });
    expect(fontOk).toBe(true);
    expect(await (await page.request.get(SUB + 'robots.txt')).text()).toContain('https://morohn.github.io/lyst/sitemap.xml');
    expect(errors).toEqual([]);
  });
});
