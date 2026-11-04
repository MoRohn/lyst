import { test, expect } from '@playwright/test';

test('mobile menu opens, traps nothing, closes on Escape and on navigation', async ({ page }) => {
  await page.goto('/');
  const toggle = page.getByRole('button', { name: 'Open menu' });
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeHidden();
  await toggle.click();
  const menu = page.getByRole('navigation', { name: 'Mobile' });
  await expect(menu).toBeVisible();
  await expect(page.getByRole('button', { name: 'Close menu' })).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(page.getByRole('button', { name: 'Open menu' })).toBeFocused();

  await page.getByRole('button', { name: 'Open menu' }).click();
  await menu.getByRole('link', { name: 'Capabilities' }).click();
  await expect(menu).toBeHidden();
  await expect(page).toHaveURL(/#capabilities$/);
  await expect(page.locator('#capabilities')).toBeInViewport();
});

test('preview is usable on a phone', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  const preview = page.locator('[data-preview]');
  await preview.scrollIntoViewIfNeeded();
  for (let i = 0; i < 5; i++) await preview.getByRole('button', { name: 'Next stage' }).click();
  await expect(preview.locator('#pv-panel-evidence')).toBeVisible();
  const box = await preview.boundingBox();
  const vw = page.viewportSize()!.width;
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(vw + 1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
