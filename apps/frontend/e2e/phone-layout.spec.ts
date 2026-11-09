import { type APIRequestContext, type Locator, type Page } from '@playwright/test';
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

  test('a conversation row keeps room for its touch actions when touched', async ({ page }) => {
    await page.goto('/source');
    await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Expand task list', exact: true }).click();
    const row = page
      .locator('#agents-sidebar')
      .getByRole('link', { name: 'Other conversation', exact: true });
    await expect(row).toBeVisible();
    const padding = () => row.evaluate((element) => getComputedStyle(element).paddingRight);
    // Pin, archive and delete always show on a touch screen, so a hovered or
    // focused title must not run underneath them.
    expect(await padding()).toBe('144px');
    await row.hover();
    expect(await padding()).toBe('144px');
    await row.focus();
    expect(await padding()).toBe('144px');
  });
});

test('a desktop window keeps the footer and the activity label', async ({ page }) => {
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  await expect(page.locator('footer')).toBeVisible();
  await expect(page.getByText('Activity: Standard', { exact: true })).toBeVisible();
});

test('a desktop row fits a long title beside a previous-year date and its actions', async ({
  page,
}) => {
  const longTitle = 'A long conversation title that fills the available sidebar width';
  await page.clock.setFixedTime(new Date('2026-11-09T12:00:00Z'));
  await page.route('**/api/v1/agent/conversations?*', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const row of body.conversations) {
      if (row.job.id === 'other') {
        row.job.title = row.title = longTitle;
        row.last_activity_at = '2025-12-31T12:00:00Z';
      }
    }
    await route.fulfill({ response, json: body });
  });
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  const row = page.locator('#agents-sidebar').getByRole('link', { name: longTitle, exact: true });
  const title = row.getByText(longTitle, { exact: true });
  const date = row.locator('..').getByText('12/31/2025', { exact: true });
  const pin = row.locator('..').getByRole('button', { name: `Pin ${longTitle}`, exact: true });
  await expect(row).toBeVisible();
  await page.mouse.move(0, 0);
  const left = (locator: Locator) =>
    locator.evaluate((element) => element.getBoundingClientRect().left);
  const titleRight = () => title.evaluate((element) => element.getBoundingClientRect().right);
  const restRight = await titleRight();
  await expect(date).toBeVisible();
  expect(restRight).toBeLessThan(await left(date));

  await row.hover();
  await expect(date).toBeHidden();
  expect(await titleRight()).toBeLessThan(restRight);
  expect(await titleRight()).toBeLessThan(await left(pin));

  await page.mouse.move(0, 0);
  await row.focus();
  await expect(date).toBeHidden();
  expect(await titleRight()).toBeLessThan(await left(pin));
});
