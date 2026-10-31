import { expect, test } from '@playwright/test';

// One model id on two of the owner's connections: the picker must keep the
// two sources apart, and a turn names the connection it was given.
const connectionId = `mconn_${'a'.repeat(32)}`;
const otherId = `mconn_${'b'.repeat(32)}`;
const personalLabel = 'fixture-model · Personal inference';
const otherLabel = 'fixture-model · Second provider';
const personal = {
  id: connectionId,
  name: 'Personal inference',
  models: ['fixture-model'],
  model_runtimes: { 'fixture-model': ['codex'] },
};
const other = { ...personal, id: otherId, name: 'Second provider' };

test.beforeEach(async ({ request, page }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.addInitScript(() => localStorage.setItem('agents.locale', 'en'));
});

test.afterEach(async ({ request }) => {
  expect((await (await request.get('/__fixture/state')).json()).unknown).toEqual([]);
});

for (const unavailable of [false, true]) {
  test(`personal source survives reload ${unavailable ? 'and becomes unavailable' : 'and can switch to the same model on another connection'}`, async ({
    page,
  }, testInfo) => {
    let enabled = true;
    const submitted: Record<string, unknown>[] = [];
    await page.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: ['fixture/repository'],
          runtimes: ['codex'],
          default_model: 'fixture-model',
          default_model_connection_id: otherId,
          github_connected: true,
          github_install_url: null,
          model_connections_enabled: true,
          model_connections: enabled ? [personal, other] : [other],
        },
      }),
    );
    await page.route('**/api/v1/agent/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      submitted.push(route.request().postDataJSON());
      // Refuse execution deliberately: this test checks the real composer's
      // outbound choice, without fabricating a running upstream provider.
      await route.fulfill({
        status: 422,
        json: { detail: 'Browser fixture captured the choice.' },
      });
    });
    await page.goto('/?repo=fixture%2Frepository');
    const picker = page.getByRole('button', { name: 'Model', exact: true });
    await picker.click();
    await expect(page.getByRole('option', { name: otherLabel, exact: true })).toBeVisible();
    await expect(page.getByRole('option', { name: personalLabel, exact: true })).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath('distinct-model-sources.png'),
      animations: 'disabled',
    });
    await page.getByRole('option', { name: personalLabel, exact: true }).click();
    await page.locator('#new-task-prompt').fill('Use the chosen provider');
    enabled = !unavailable;
    await page.reload();
    await expect(page.locator('#new-task-prompt')).toHaveValue('Use the chosen provider');

    if (unavailable) {
      await expect(picker).toContainText('unavailable');
      await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled();
      await page.locator('#new-task-prompt').press('Enter');
      expect(submitted).toEqual([]);
      // Never swapped for the other connection's same model, now its only label.
      await expect(picker).not.toHaveText('fixture-model');
      return;
    }

    await expect(picker).toContainText(personalLabel);
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => submitted.length).toBe(1);
    expect(submitted[0]).toMatchObject({
      model: 'fixture-model',
      model_connection_id: connectionId,
    });
    await expect(page.locator('#new-task-prompt')).toBeEnabled();
    await picker.click();
    await page.getByRole('option', { name: otherLabel, exact: true }).click();
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => submitted.length).toBe(2);
    expect(submitted[1]).toMatchObject({ model: 'fixture-model', model_connection_id: otherId });
  });
}
