import { expect, test, type Page } from '@playwright/test';

// Admin → Users in the production build: the old Accounts address lands on it,
// it is the first admin section, its quick views write the address bar, and
// on a phone the table scrolls inside its card rather than the page.

const lead = {
  id: '01LEAD',
  email: 'lead@example.test',
  email_verified: true,
  status: 'active',
  is_admin: true,
  waitlist: {
    status: 'approved',
    purpose: null,
    review_reason: null,
    version: 3,
    submitted_at: '2026-10-20T00:00:00Z',
    reviewed_at: '2026-10-21T00:00:00Z',
  },
  created_at: '2026-10-20T00:00:00Z',
  last_login_at: '2026-11-02T00:00:00Z',
  identity: { label: 'lead@example.test', user_name: 'Grace Hopper', identity_verified: true },
  teams: [
    { id: 'team-lab', name: 'Compiler lab', role: 'owner' },
    { id: 'team-side', name: 'Side project', role: 'member' },
  ],
};
const applicant = {
  ...lead,
  id: '01APPLICANT',
  email: 'applicant@example.test',
  is_admin: false,
  waitlist: {
    ...lead.waitlist,
    status: 'pending',
    purpose: 'Coursework for a systems class, mostly small repositories.',
    reviewed_at: null,
  },
  last_login_at: null,
  identity: { label: 'applicant@example.test', user_name: null, identity_verified: true },
  teams: [],
};

async function asAdministrator(page: Page, listed: URL[]) {
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      json: {
        id: 'browser-owner',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    }),
  );
  await page.route('**/api/admin/agent/settings', (route) =>
    route.fulfill({
      json: {
        revision: 1,
        definitions: [],
        values: {},
        secret_configured: {},
        bootstrap: [],
        setup: { required: false, groups: [], bootstrap_missing: [] },
      },
    }),
  );
  await page.route('**/api/v1/admin/accounts?*', (route) => {
    const url = new URL(route.request().url());
    listed.push(url);
    const pending = url.searchParams.get('status') === 'pending';
    route.fulfill({
      json: {
        accounts: pending ? [applicant] : [lead, applicant],
        total: pending ? 1 : 2,
        counts: { all: 2, 'admission:pending': 1, 'admission:approved': 1 },
      },
    });
  });
}

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
});

test('the old Accounts address opens Users, the first admin section', async ({ page }) => {
  const listed: URL[] = [];
  await asAdministrator(page, listed);
  await page.goto('/admin/accounts');

  await expect(page).toHaveURL(/\/admin\/users$/);
  const sections = page.getByRole('navigation', { name: 'Admin sections' }).getByRole('link');
  await expect(sections.first()).toHaveText('Users');
  await expect(sections.first()).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('link', { name: 'Compiler lab (owner)' })).toHaveAttribute(
    'href',
    '/admin/users/teams/team-lab',
  );

  await page
    .getByRole('group', { name: 'Account counts' })
    .getByRole('button', { name: /Waiting/ })
    .click();
  await expect(page).toHaveURL(/\/admin\/users\?status=pending$/);
  await expect(page.getByText('lead@example.test')).toHaveCount(0);
  expect(listed.at(-1)?.searchParams.get('status')).toBe('pending');

  // A reload keeps the view it was on.
  await page.reload();
  await expect(
    page.getByRole('group', { name: 'Account counts' }).getByRole('button', { name: /Waiting/ }),
  ).toHaveAttribute('aria-pressed', 'true');
});

test.describe('on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('the table scrolls inside its card, not the page', async ({ page }) => {
    await asAdministrator(page, []);
    await page.goto('/admin/users');
    await expect(page.getByRole('table')).toBeVisible();

    const overflow = await page.evaluate(() => {
      const workspace = document.getElementById('agents-workspace')!;
      const table = document.querySelector('table')!.parentElement!;
      return {
        page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        workspace: workspace.scrollWidth - workspace.clientWidth,
        table: table.scrollWidth - table.clientWidth,
      };
    });
    expect(overflow.page).toBeLessThanOrEqual(0);
    expect(overflow.workspace).toBeLessThanOrEqual(0);
    expect(overflow.table).toBeGreaterThan(0);
  });
  test('an open account fits the phone, however far the table is scrolled', async ({ page }) => {
    await asAdministrator(page, []);
    await page.goto('/admin/users');
    const scroller = page.locator('table').locator('..');
    await scroller.evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
    });
    // The chevron is a real button: the keyboard opens the row as a click does.
    const toggle = page.getByRole('button', { name: 'Details for applicant@example.test' });
    await toggle.focus();
    await page.keyboard.press('Enter');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const panel = page.locator('#account-detail-01APPLICANT > td > div');
    await expect(panel.getByRole('combobox', { name: 'Action' })).toBeVisible();

    const fit = await page.evaluate(() => {
      const box = document
        .querySelector('#account-detail-01APPLICANT > td > div')!
        .getBoundingClientRect();
      const action = document.getElementById('account-action')!.getBoundingClientRect();
      const scroller = document.querySelector('table')!.parentElement!.getBoundingClientRect();
      return {
        page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        panelLeft: box.left - scroller.left,
        panelRight: scroller.right - box.right,
        actionInside: action.left >= scroller.left && action.right <= scroller.right,
      };
    });
    expect(fit.page).toBeLessThanOrEqual(0);
    expect(Math.abs(fit.panelLeft)).toBeLessThanOrEqual(1);
    expect(Math.abs(fit.panelRight)).toBeLessThanOrEqual(1);
    expect(fit.actionInside).toBe(true);
    await expect(panel.getByText('Coursework for a systems class', { exact: false })).toBeVisible();
  });
});
