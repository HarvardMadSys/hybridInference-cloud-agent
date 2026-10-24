import { type APIRequestContext, type Page } from '@playwright/test';
import { expect, test } from './network-change';

// Only a real browser lays the page out, so how much of a phone's screen the
// conversation keeps is measured here rather than read off class names. The
// shell's header, the job header, the footer and the sticky composer once left
// a 320 x 568 phone a strip of transcript a few pixels tall.
async function state(request: APIRequestContext) {
  return (await request.get('/__fixture/state')).json();
}
async function box(page: Page, selector: string) {
  const found = await page.locator(selector).first().boundingBox();
  if (!found) throw new Error(`Not laid out: ${selector}`);
  return found;
}

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
});
test.afterEach(async ({ request }) => {
  expect((await state(request)).unknown).toEqual([]);
});

test.describe('on a phone', () => {
  test.use({
    viewport: { width: 320, height: 568 },
    isMobile: true,
    hasTouch: true,
  });

  test('the conversation keeps a usable share of the screen', async ({ page }) => {
    await page.goto('/source');
    const follow = page.getByLabel('Add a follow-up', { exact: true });
    await expect(follow).toBeVisible();

    // The nav shares the brand's row rather than taking one of its own.
    const brand = await box(page, 'header a[href="/"]');
    const nav = await box(page, 'header nav');
    expect(Math.abs(brand.y + brand.height / 2 - (nav.y + nav.height / 2))).toBeLessThan(2);
    // The credits and build line stay on larger screens only.
    await expect(page.locator('footer')).toBeHidden();
    // The run controls fit one row: the view tabs sit beside the workspace toggle.
    const tabs = await box(page, 'nav[aria-label="Run views"]');
    const workspace = await box(page, 'button[aria-controls="job-workspace-pane"]');
    expect(Math.abs(tabs.y + tabs.height / 2 - (workspace.y + workspace.height / 2))).toBeLessThan(
      2,
    );

    const reading = await page.evaluate(() => {
      const region = document.querySelector('[role="region"][aria-label="Task progress"]');
      const composer = document
        .querySelector('textarea[aria-label="Add a follow-up"]')
        ?.closest('.sticky');
      if (!region || !composer) return 0;
      return composer.getBoundingClientRect().top - region.getBoundingClientRect().top;
    });
    // About 180 px today; the strip this guards against was under 10.
    expect(reading).toBeGreaterThanOrEqual(568 / 4);
  });
});

test('a desktop window keeps the footer and the activity label', async ({ page }) => {
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  await expect(page.locator('footer')).toBeVisible();
  await expect(page.getByText('Activity: Standard', { exact: true })).toBeVisible();
});
