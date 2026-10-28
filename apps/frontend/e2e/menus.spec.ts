import { expect, test } from '@playwright/test';

// The shared menu surfaces, exercised through the real specialty controls:
// the composer's model and agent pickers, the repository menu with its search
// and footer, and the shell's account, view and task action menus. The fixture
// serves every API response, so no provider is called.
/** Let the page's own entrance animation finish before a review snapshot. */
async function settled(page: import('@playwright/test').Page) {
  await page.locator('main').evaluate(async (element) => {
    await Promise.all(
      element.getAnimations({ subtree: true }).map((animation) => animation.finished),
    );
  });
}

const variants = [
  { name: 'desktop light', width: 1440, height: 900, theme: 'light' },
  { name: 'mobile dark', width: 390, height: 844, theme: 'dark' },
] as const;

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      json: {
        id: 'browser-owner',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    }),
  );
});

test.afterEach(async ({ request }) => {
  const state = await (await request.get('/__fixture/state')).json();
  expect(state.unknown).toEqual([]);
  expect(state.creates).toEqual([]);
});

for (const variant of variants) {
  test(`composer pickers and repository menu share one surface (${variant.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: variant.width, height: variant.height });
    await page.addInitScript((theme) => localStorage.setItem('agents.theme', theme), variant.theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/');
    await settled(page);
    const repository = page.getByRole('button', { name: /^Repository:/ });
    await expect(repository).toBeVisible();

    const model = page.getByRole('button', { name: 'Model', exact: true });
    await model.click();
    const modelPanel = page.getByRole('dialog', { name: 'Model', exact: true });
    await expect(modelPanel).toBeVisible();
    await expect(modelPanel.getByRole('option', { name: 'fixture-model' })).toBeVisible();
    const modelBox = await modelPanel.boundingBox();
    expect(modelBox).not.toBeNull();
    expect(modelBox!.x).toBeGreaterThanOrEqual(0);
    expect(modelBox!.x + modelBox!.width).toBeLessThanOrEqual(variant.width);
    await page.screenshot({
      path: testInfo.outputPath(`composer-model-picker-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('Escape');
    await expect(modelPanel).toHaveCount(0);
    await expect(model).toBeFocused();

    const agent = page.getByRole('button', { name: 'Agent', exact: true });
    await agent.click();
    const agentPanel = page.getByRole('dialog', { name: 'Agent', exact: true });
    await expect(agentPanel).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`composer-agent-picker-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await expect(agentPanel).toHaveCount(0);
    await expect(agent).toBeFocused();

    await repository.click();
    const repositoryPanel = page.getByRole('dialog', { name: 'Repository', exact: true });
    await expect(repositoryPanel).toBeVisible();
    const search = repositoryPanel.getByRole('searchbox', { name: 'Search repositories…' });
    await expect(search).toBeFocused();
    await page.screenshot({
      path: testInfo.outputPath(`repository-menu-${variant.name.replace(' ', '-')}.png`),
    });
    await search.fill('fixture');
    await expect(
      repositoryPanel.getByRole('button', { name: 'fixture/repository', exact: true }),
    ).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(repositoryPanel).toHaveCount(0);
    await expect(repository).toBeFocused();
    await expect(page.locator('select, datalist')).toHaveCount(0);
  });

  test(`shell and task action menus share one surface (${variant.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: variant.width, height: variant.height });
    await page.addInitScript((theme) => localStorage.setItem('agents.theme', theme), variant.theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/source');
    await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
    await settled(page);

    if (variant.name === 'mobile dark') {
      await page.getByRole('button', { name: 'Expand task list', exact: true }).click();
    }
    const account = page.getByRole('button', { name: /^Account menu/ }).first();
    await account.click();
    const accountMenu = page.getByRole('menu', { name: 'Account menu' });
    await expect(accountMenu).toBeVisible();
    const accountBox = await accountMenu.boundingBox();
    expect(accountBox).not.toBeNull();
    expect(accountBox!.x).toBeGreaterThanOrEqual(0);
    expect(accountBox!.y).toBeGreaterThanOrEqual(0);
    expect(accountBox!.x + accountBox!.width).toBeLessThanOrEqual(variant.width);
    expect(accountBox!.y + accountBox!.height).toBeLessThanOrEqual(variant.height);
    await page.screenshot({
      path: testInfo.outputPath(`account-menu-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('Escape');
    await expect(accountMenu).toHaveCount(0);
    await expect(account).toBeFocused();

    await page.getByRole('button', { name: 'View options', exact: true }).click();
    const viewMenu = page.getByRole('menu', { name: 'View options' });
    await expect(viewMenu).toBeVisible();
    await expect(viewMenu.getByRole('menuitemradio')).toHaveCount(4);
    await page.screenshot({
      path: testInfo.outputPath(`sidebar-view-menu-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('Escape');
    await expect(viewMenu).toHaveCount(0);

    if (variant.name === 'mobile dark') {
      await page.getByRole('button', { name: 'Collapse task list', exact: true }).click();
    }
    const actions = page.getByRole('button', { name: 'Task actions', exact: true });
    await actions.click();
    const actionsMenu = page.getByRole('menu', { name: 'Task actions' });
    await expect(actionsMenu).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`task-actions-menu-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('Escape');
    await expect(actionsMenu).toHaveCount(0);
    await expect(actions).toBeFocused();
  });
}

for (const variant of variants) {
  test(`admin form Select matches the same surface (${variant.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: variant.width, height: variant.height });
    await page.addInitScript((theme) => localStorage.setItem('agents.theme', theme), variant.theme);
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
    await page.route('**/api/admin/agent/database-tools', (route) =>
      route.fulfill({ json: { enabled: false, available: false, url: null } }),
    );
    await page.route('**/api/admin/agent/database/tables', (route) =>
      route.fulfill({
        json: {
          tables: ['agent_jobs', 'agent_threads', 'agent_attempts'].map((name) => ({
            name,
            columns: [{ name: 'id', data_type: 'uuid', nullable: false }],
            primary_key: ['id'],
          })),
        },
      }),
    );
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/admin/database');
    await settled(page);
    const table = page.getByRole('combobox', { name: 'Table', exact: true });
    await table.click();
    const listbox = page.getByRole('listbox').first();
    await expect(listbox).toBeVisible();
    const bounds = await listbox.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(variant.width);
    await page.screenshot({
      path: testInfo.outputPath(`admin-select-${variant.name.replace(' ', '-')}.png`),
      animations: 'disabled',
    });
    await page.keyboard.press('Escape');
    await expect(page.getByRole('listbox')).toHaveCount(0);
    await expect(table).toBeFocused();
  });
}
