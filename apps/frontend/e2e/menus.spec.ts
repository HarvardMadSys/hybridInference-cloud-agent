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

for (const variant of variants) {
  test(`action menus focus their first and last row (${variant.name})`, async ({ page }) => {
    await page.setViewportSize({ width: variant.width, height: variant.height });
    await page.addInitScript((theme) => localStorage.setItem('agents.theme', theme), variant.theme);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/source');
    await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
    await settled(page);

    // On a phone the task list starts collapsed, so the job header is visible.
    const actions = page.getByRole('button', { name: 'Task actions', exact: true });
    await actions.click();
    const menu = page.getByRole('menu', { name: 'Task actions' });
    await expect(menu).toBeVisible();
    // A browser cannot focus a row while the panel is still visibility:hidden.
    await expect(menu.getByRole('menuitem').first()).toBeFocused();

    await page.keyboard.press('End');
    await expect(menu.getByRole('menuitem').last()).toBeFocused();
    await page.keyboard.press('Home');
    await expect(menu.getByRole('menuitem').first()).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveCount(0);
    await expect(actions).toBeFocused();
  });
}

test('a long repository list scrolls inside its bounded panel above the footer', async ({
  page,
  request,
}) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  const repos = Array.from({ length: 50 }, (_, index) => `owner/repository-${index}`);
  await page.route('**/api/v1/agent/config', (route) =>
    route.fulfill({
      json: {
        repos,
        runtimes: ['codex'],
        default_model: 'fixture-model',
        github_connected: true,
        github_install_url: null,
      },
    }),
  );
  await page.setViewportSize({ width: 390, height: 480 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await settled(page);
  const repository = page.getByRole('button', { name: /^Repository:/ });
  await repository.click();
  const panel = page.getByRole('dialog', { name: 'Repository', exact: true });
  const search = panel.getByRole('searchbox', { name: 'Search repositories…' });
  await expect(search).toBeFocused();

  const panelBox = await panel.boundingBox();
  expect(panelBox).not.toBeNull();
  expect(panelBox!.y).toBeGreaterThanOrEqual(0);
  expect(panelBox!.y + panelBox!.height).toBeLessThanOrEqual(480);
  // The footer stays inside the panel and the rows scroll under it.
  const footer = panel.getByRole('button', { name: 'Refresh', exact: true });
  await expect(footer).toBeVisible();
  const footerBox = await footer.boundingBox();
  expect(footerBox!.y + footerBox!.height).toBeLessThanOrEqual(panelBox!.y + panelBox!.height);

  const list = panel.locator('div[class*="overflow-y-auto"]').first();
  const scroll = await list.evaluate((element) => ({
    client: element.clientHeight,
    total: element.scrollHeight,
    overflow: getComputedStyle(element).overflowY,
  }));
  expect(scroll.overflow).toBe('auto');
  expect(scroll.total).toBeGreaterThan(scroll.client);

  // The last row is reachable, which a clipped panel would prevent.
  const last = panel.getByRole('button', { name: 'owner/repository-49', exact: true });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeVisible();
  await expect(footer).toBeVisible();
});

test('a real containing block does not displace the task actions menu', async ({
  page,
  request,
}) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  await settled(page);
  // `will-change: transform` makes the header a real containing block for its
  // inline fixed-position menu without moving anything. The correction has to
  // find that origin; a transform on an unrelated ancestor would prove nothing.
  await page.locator('header.agent-conversation-header').evaluate((element) => {
    (element as HTMLElement).style.willChange = 'transform';
  });
  const actions = page.getByRole('button', { name: 'Task actions', exact: true });
  const trigger = await actions.boundingBox();
  await actions.click();
  const menu = page.getByRole('menu', { name: 'Task actions' });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole('menuitem').first()).toBeFocused();
  // The first visible frame is already corrected: no flash at the header origin.
  const firstFrameGap = await menu.evaluate((element, box) => {
    const rect = element.getBoundingClientRect();
    return box ? Math.round(rect.top - (box.y + box.height)) : null;
  }, trigger);
  expect(firstFrameGap).toBe(4);
  const box = await menu.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(900);
});

test('scrolling keeps a fixed picker beside its trigger without overshoot', async ({
  page,
  request,
}) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.setViewportSize({ width: 1024, height: 560 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.route('**/api/v1/agent/config', (route) =>
    route.fulfill({
      json: {
        repos: ['fixture/repository'],
        runtimes: ['codex'],
        default_model: 'model-a',
        default_model_connection_id: `mconn_${'c'.repeat(32)}`,
        model_connections_enabled: true,
        model_connections: [
          {
            id: `mconn_${'c'.repeat(32)}`,
            name: 'Fixture provider',
            models: ['model-a', 'model-b', 'model-c', 'model-d', 'model-e'],
            model_runtimes: Object.fromEntries(
              ['model-a', 'model-b', 'model-c', 'model-d', 'model-e'].map((model) => [
                model,
                ['codex'],
              ]),
            ),
          },
        ],
        github_connected: true,
        github_install_url: null,
      },
    }),
  );
  await page.goto('/');
  await settled(page);
  await page.getByRole('button', { name: 'Model', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Model', exact: true });
  await expect(panel).toBeVisible();

  // Sample every animation frame while the wheel scrolls the page underneath.
  const samples = await page.evaluate(async () => {
    const model = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
      (button) => button.getAttribute('aria-label') === 'Model',
    );
    const dialog = document.querySelector<HTMLElement>('[role="dialog"][aria-label="Model"]');
    const main = document.querySelector('main');
    if (!model || !dialog || !main) throw new Error('Menu or trigger missing');
    const gaps: number[] = [];
    let stop = false;
    // Positive gap means the panel sits that far outside its trigger's nearest
    // edge; a value above its 6px gap is the overshoot this guards.
    const sample = () => {
      const trigger = model.getBoundingClientRect();
      const box = dialog.getBoundingClientRect();
      gaps.push(
        Math.round(box.top >= trigger.bottom ? box.top - trigger.bottom : trigger.top - box.bottom),
      );
      if (!stop) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
    for (let step = 0; step < 6; step += 1) {
      main.scrollBy(0, 40);
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }
    await new Promise((resolve) => setTimeout(resolve, 120));
    stop = true;
    return gaps;
  });
  // Every sampled frame keeps the ordinary 6px gap: placement is written to the
  // live element before paint, so there is neither the old full-delta overshoot
  // (46px here) nor a lagging frame.
  expect(samples.at(-1)).toBe(6);
  expect(Math.min(...samples)).toBeGreaterThanOrEqual(5);
  expect(Math.max(...samples)).toBeLessThanOrEqual(7);
});

test('hovering one model and arrowing to another highlights only the active row', async ({
  page,
  request,
}) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.route('**/api/v1/agent/config', (route) =>
    route.fulfill({
      json: {
        repos: ['fixture/repository'],
        runtimes: ['codex'],
        default_model: 'model-a',
        default_model_connection_id: `mconn_${'c'.repeat(32)}`,
        model_connections_enabled: true,
        model_connections: [
          {
            id: `mconn_${'c'.repeat(32)}`,
            name: 'Fixture provider',
            models: ['model-a', 'model-b', 'model-c', 'model-d'],
            model_runtimes: Object.fromEntries(
              ['model-a', 'model-b', 'model-c', 'model-d'].map((model) => [model, ['codex']]),
            ),
          },
        ],
        github_connected: true,
        github_install_url: null,
      },
    }),
  );
  await page.goto('/');
  await settled(page);
  await page.getByRole('button', { name: 'Model', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Model', exact: true });
  const option = (name: string) => panel.getByRole('option', { name, exact: true });
  await option('model-b').hover();
  // Pointer movement makes b active; two ArrowDowns move the active row to d.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  const backgrounds = await panel.locator('[role="option"]').evaluateAll((rows) =>
    rows.map((row) => ({
      name: row.getAttribute('aria-label') ?? row.textContent,
      background: getComputedStyle(row).backgroundColor,
      hover: row.matches(':hover'),
    })),
  );
  const byName = Object.fromEntries(backgrounds.map((row) => [row.name, row]));
  // The selected model keeps its accent; only the active row gets the hover tint.
  expect(byName['model-a'].background).not.toBe('rgba(0, 0, 0, 0)');
  expect(byName['model-d'].background).toBe('rgb(240, 240, 243)');
  expect(byName['model-b'].background).toBe('rgba(0, 0, 0, 0)');
  expect(byName['model-c'].background).toBe('rgba(0, 0, 0, 0)');
  expect(backgrounds.filter((row) => row.hover).map((row) => row.name)).toEqual(['model-b']);
});
test('account navigation links and repository footer anchors highlight on hover', async ({
  page,
  request,
}) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  await settled(page);

  const account = page.getByRole('button', { name: /^Account menu/ }).first();
  await account.click();
  const menu = page.getByRole('menu', { name: 'Account menu' });
  await expect(menu).toBeVisible();
  // A navigation row is a link, not a form control: :enabled never matched it.
  const settings = menu.getByRole('menuitem', { name: 'Switch account', exact: true });
  await page.mouse.move(0, 0);
  expect(await settings.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(
    'rgba(0, 0, 0, 0)',
  );
  await settings.hover();
  await expect
    .poll(() => settings.evaluate((element) => getComputedStyle(element).backgroundColor))
    .toBe('rgb(240, 240, 243)');
  await page.keyboard.press('Escape');

  // The repository menu belongs to the new-task composer, not a conversation.
  await page.goto('/');
  await expect(page.getByRole('button', { name: /^Repository:/ })).toBeVisible();
  await settled(page);
  await page.getByRole('button', { name: /^Repository:/ }).click();
  const repository = page.getByRole('dialog', { name: 'Repository', exact: true });
  const footer = repository.getByRole('link', { name: 'Create new repository', exact: true });
  await page.mouse.move(0, 0);
  expect(await footer.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(
    'rgba(0, 0, 0, 0)',
  );
  await footer.hover();
  await expect
    .poll(() => footer.evaluate((element) => getComputedStyle(element).backgroundColor))
    .toBe('rgb(240, 240, 243)');
});

test('action rows keep a common inset and open from trigger arrows', async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/source');
  await expect(page.getByLabel('Add a follow-up', { exact: true })).toBeVisible();
  await settled(page);

  const actions = page.getByRole('button', { name: 'Task actions', exact: true });
  await actions.focus();
  await page.keyboard.press('ArrowDown');
  const menu = page.getByRole('menu', { name: 'Task actions' });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole('menuitem').first()).toBeFocused();

  const panelBox = await menu.boundingBox();
  const firstRow = await menu.getByRole('menuitem').first().boundingBox();
  expect(panelBox).not.toBeNull();
  expect(firstRow).not.toBeNull();
  // The shared 6px body inset, on each side and above the first row.
  expect(Math.round(firstRow!.x - panelBox!.x)).toBeGreaterThanOrEqual(6);
  expect(Math.round(firstRow!.y - panelBox!.y)).toBeGreaterThanOrEqual(6);

  await page.keyboard.press('ArrowUp');
  await expect(menu.getByRole('menuitem').last()).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(actions).toBeFocused();
});

test('the embedded SSH list draws one card, unlike the outer add menu', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await settled(page);
  await page.getByRole('button', { name: 'Add to message' }).click();
  await page.getByRole('menuitem', { name: /SSH servers/ }).click();
  const panel = page.getByRole('dialog', { name: 'SSH servers' });
  await expect(panel).toBeVisible();
  const cards = await panel.locator('.shadow-card').count();
  expect(cards).toBe(0);
});
