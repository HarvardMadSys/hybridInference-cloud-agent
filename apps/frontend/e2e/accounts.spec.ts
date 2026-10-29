import { expect, test } from '@playwright/test';

const pending = {
  status: 'pending',
  purpose: 'Research tooling',
  review_reason: null,
  version: 1,
  submitted_at: '2026-10-28T00:00:00Z',
  reviewed_at: null,
};

test.beforeEach(async ({ request, page }) => {
  await request.post('/__fixture/reset');
  await page.route('**/api/v1/auth/config', (route) =>
    route.fulfill({ json: { mode: 'local', signup_enabled: true } }),
  );
});

test('independent login reaches the waiting list without mounting protected Agent work', async ({
  page,
}, info) => {
  let signedIn = false;
  const protectedCalls: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/v1/agent/')) protectedCalls.push(request.url());
  });
  const user = {
    id: 'local-applicant',
    auth_mode: 'local',
    email: 'applicant@example.test',
    agent_access: { allowed: false, permissions: [] },
    waitlist: pending,
  };
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      status: signedIn ? 200 : 401,
      json: signedIn ? user : { detail: 'Not signed in' },
    }),
  );
  await page.route('**/api/v1/auth/login', (route) => {
    expect(route.request().postDataJSON()).toEqual({
      email: 'applicant@example.test',
      password: 'Example123',
    });
    signedIn = true;
    return route.fulfill({ json: user });
  });
  await page.route('**/api/v1/waitlist', (route) => route.fulfill({ json: pending }));
  await page.goto('/login');
  await page.getByLabel('Email', { exact: true }).fill('applicant@example.test');
  await page.getByLabel('Password', { exact: true }).fill('Example123');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL(/\/waiting-list$/);
  await expect(page.getByText('Waiting for review', { exact: true })).toBeVisible();
  await expect(page.getByText('Research tooling', { exact: true })).toBeVisible();
  expect(protectedCalls).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    .toBe(true);
  await page.screenshot({ path: info.outputPath('waiting-list-mobile.png'), fullPage: true });
});

test('signup asks for the Terms, validates the reused password policy and explains the email step', async ({
  page,
}, info) => {
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
        terms_text: 'Be kind.',
        setup_required: false,
        max_accounts_per_browser: 5,
      },
    }),
  );
  const sent: unknown[] = [];
  await page.route('**/api/v1/auth/signup', (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/signup');
  // Nothing can be sent before the Terms of Service are agreed to.
  await expect(page.getByRole('button', { name: 'Create account', exact: true })).toBeDisabled();
  await page.getByLabel('How will you use Cloud Agent?').fill('Research tooling');
  await page.getByRole('checkbox').check();
  await page.getByLabel('Email', { exact: true }).fill('new@example.test');
  await page.getByLabel('Password', { exact: true }).fill('Example123');
  await page.getByLabel('Confirm password', { exact: true }).fill('Different123');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Passwords do not match.' })).toHaveText(
    'Passwords do not match.',
  );
  expect(sent).toEqual([]);
  await page.getByLabel('Confirm password', { exact: true }).fill('Example123');
  await page.screenshot({ path: info.outputPath('signup-desktop.png'), fullPage: true });
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

test('verification waits for a click and keeps its token out of network URLs', async ({ page }) => {
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Not signed in' } }),
  );
  const sent: unknown[] = [];
  const urls: string[] = [];
  page.on('request', (request) => urls.push(request.url()));
  await page.route('**/api/v1/auth/verify-email', (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/verify-email#token=verification-fixture-secret');
  await expect(page.getByRole('button', { name: 'Verify email', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/verify-email$/);
  expect(sent).toEqual([]);
  await page.getByRole('button', { name: 'Verify email', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('Email verified.');
  expect(sent).toEqual([{ token: 'verification-fixture-secret' }]);
  expect(urls.every((url) => !url.includes('verification-fixture-secret'))).toBe(true);
});
