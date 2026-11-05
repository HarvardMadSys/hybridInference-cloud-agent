import { expect, test, type Page } from '@playwright/test';

// Placeholder Terms, long enough to scroll: the deployment's own wording is
// configuration, not part of this repository.
const CLAUSE = 'This placeholder clause stands in for the text a deployment publishes. '
  .repeat(5)
  .trim();
const SECTIONS = Array.from(
  { length: 24 },
  (_, index) => `${index + 1}. Example section ${index + 1}. ${CLAUSE}`,
);
const TERMS = ['Last updated: January 1, 2026.', ...SECTIONS].join('\n\n');

async function serve(page: Page, sent: unknown[]) {
  await page.route('**/api/v1/auth/config', (route) =>
    route.fulfill({ json: { signup_enabled: true } }),
  );
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Not signed in' } }),
  );
  await page.route('**/api/v1/auth/options', (route) =>
    route.fulfill({
      json: {
        signup_enabled: true,
        phone_signup_enabled: false,
        email_delivery: true,
        require_email_verification: true,
        phone: { enabled: false, default_country_code: '', allowed_prefixes: [] },
        turnstile_site_key: '',
        terms_url: '',
        terms_text: TERMS,
        setup_required: false,
        max_accounts_per_browser: 5,
      },
    }),
  );
  await page.route('**/api/v1/auth/signup', (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
}

const fitsWidth = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test.beforeEach(async ({ request }) => {
  await request.post('/__fixture/reset');
});

test('sign-up Terms are read to the end by keyboard and agreed to without leaving the form', async ({
  page,
}, info) => {
  const sent: unknown[] = [];
  await serve(page, sent);
  await page.goto('/signup');
  const dialog = page.getByRole('dialog', { name: 'Terms of Service' });
  const text = dialog.getByRole('region', { name: 'Terms of Service text' });
  const agree = dialog.getByRole('button', { name: 'Agree', exact: true });
  await expect(text).toBeFocused();
  await expect(dialog.getByText('Last updated: January 1, 2026.')).toBeVisible();
  await expect(agree).toBeDisabled();
  await page.screenshot({ path: info.outputPath('terms-dialog-start.png') });

  await page.keyboard.press('PageDown');
  await expect(agree).toBeDisabled();
  await page.keyboard.press('End');
  await expect(agree).toBeEnabled();
  await expect(dialog.getByText('You have reached the end of the Terms.')).toBeVisible();

  // Tab stays inside the dialog.
  for (let step = 0; step < 6; step += 1) {
    await page.keyboard.press('Tab');
    expect(
      await page.evaluate(() =>
        Boolean(document.querySelector('[role="dialog"]')?.contains(document.activeElement)),
      ),
    ).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  const opener = page.getByRole('button', { name: 'Read and agree', exact: true });
  await expect(opener).toBeFocused();
  await expect(page).toHaveURL(/\/signup$/);

  await page.getByLabel('How will you use Cloud Agent?').fill('Research tooling');
  await page.getByLabel('Email', { exact: true }).fill('new@example.test');
  await page.getByLabel('Password', { exact: true }).fill('Example123');
  await page.getByLabel('Confirm password', { exact: true }).fill('Example123');
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeDisabled();

  await opener.click();
  await expect(agree).toBeEnabled();
  await agree.click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('You agreed to the Terms of Service.')).toBeVisible();
  await expect(page.getByLabel('Email', { exact: true })).toHaveValue('new@example.test');
  await expect(page.getByLabel('How will you use Cloud Agent?')).toHaveValue('Research tooling');
  expect(sent).toEqual([]);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
  expect(sent).toEqual([
    {
      email: 'new@example.test',
      password: 'Example123',
      use_case: 'Research tooling',
      accepted_tos: true,
    },
  ]);
});

for (const viewport of [
  { name: 'phone', width: 390, height: 844 },
  // About a 1280×720 window at 200% zoom: the whole dialog scrolls instead.
  { name: 'zoomed', width: 640, height: 360 },
]) {
  test(`the sign-up Terms dialog fits and reaches its end on a ${viewport.name} screen`, async ({
    page,
  }, info) => {
    await page.setViewportSize(viewport);
    await serve(page, []);
    await page.goto('/signup');
    const dialog = page.getByRole('dialog', { name: 'Terms of Service' });
    const agree = dialog.getByRole('button', { name: 'Agree', exact: true });
    await expect(agree).toBeDisabled();
    const box = await dialog.boundingBox();
    expect(box && box.x >= 0 && box.y >= 0).toBe(true);
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
    expect(await fitsWidth(page)).toBe(true);
    await page.screenshot({ path: info.outputPath(`terms-dialog-${viewport.name}.png`) });
    await dialog.hover();
    for (let turn = 0; turn < 40 && (await agree.isDisabled()); turn += 1)
      await page.mouse.wheel(0, 900);
    await agree.scrollIntoViewIfNeeded();
    await expect(agree).toBeEnabled();
    await agree.click();
    await expect(page.getByText('You agreed to the Terms of Service.')).toBeVisible();
  });
}

test('the Terms page sets the date and numbered sections in a reading column', async ({
  page,
}, info) => {
  await serve(page, []);
  await page.goto('/terms');
  const article = page.getByRole('article');
  await expect(article.getByRole('heading', { level: 2, name: 'Terms of Service' })).toBeVisible();
  await expect(article.getByText('Last updated: January 1, 2026.')).toBeVisible();
  await expect(article.getByRole('heading', { level: 3 })).toHaveCount(24);
  const width = (await article.boundingBox())!.width;
  expect(width).toBeLessThanOrEqual(760);
  await page.screenshot({ path: info.outputPath('terms-page-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await fitsWidth(page)).toBe(true);
});
