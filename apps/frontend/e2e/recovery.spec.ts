import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

async function configure(request: APIRequestContext, settings: Record<string, boolean>) {
  expect((await request.post('/__fixture/config', { data: settings })).ok()).toBe(true);
}
async function state(request: APIRequestContext) {
  return (await request.get('/__fixture/state')).json();
}
async function release(request: APIRequestContext, key: string, fail = false) {
  expect((await request.post('/__fixture/release', { data: { key, fail } })).ok()).toBe(true);
}
async function newTask(page: Page) {
  await page.getByRole('link', { name: 'New task', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator('#new-task-prompt')).toBeVisible();
}
async function send(page: Page, prompt: string) {
  const input = page.locator('#new-task-prompt');
  await input.fill(prompt);
  await input.press('Enter');
}
async function openFork(page: Page) {
  await page.getByRole('button', { name: 'Task actions', exact: true }).click();
  await page.getByRole('menuitem', { name: /Fork/ }).click();
}

test.beforeEach(async ({ request, page }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  page.on('dialog', (dialog) => dialog.accept());
});
test.afterEach(async ({ request }) => {
  expect((await state(request)).unknown).toEqual([]);
});

test('leaving a conversation prevents a delayed Fork from stealing navigation', async ({
  page,
  request,
}) => {
  await configure(request, { delayFork: true });
  await page.goto('/source');
  await openFork(page);
  await expect.poll(async () => (await state(request)).pending).toContain('fork');
  await newTask(page);
  await page.locator('#new-task-prompt').fill('Keep this draft');
  const response = page.waitForResponse((reply) => reply.url().endsWith('/source/fork'));
  await release(request, 'fork');
  await response;
  // A positive URL assertion alone can pass before React commits the late
  // navigation. Observe the forbidden transition through a bounded window.
  await expect(page.waitForURL(/\/fork-1$/, { timeout: 1_500 })).rejects.toThrow('Timeout');
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator('#new-task-prompt')).toHaveValue('Keep this draft');
});

test('switching conversations ignores a delayed Fork from the previous conversation', async ({
  page,
  request,
}) => {
  await configure(request, { delayFork: true });
  await page.goto('/source');
  await openFork(page);
  await expect.poll(async () => (await state(request)).pending).toContain('fork');
  await page.getByRole('link', { name: 'Other conversation', exact: true }).click();
  await expect(page).toHaveURL(/\/other$/);
  const input = page.getByRole('textbox', { name: 'Add a follow-up', exact: true });
  await input.fill('Keep the other conversation draft');
  const response = page.waitForResponse((reply) => reply.url().endsWith('/source/fork'));
  await release(request, 'fork');
  await response;
  await expect(page.waitForURL(/\/fork-1$/, { timeout: 1_500 })).rejects.toThrow('Timeout');
  await expect(page).toHaveURL(/\/other$/);
  await expect(input).toHaveValue('Keep the other conversation draft');
});

test('a late creation response preserves the newer task receipt across a lost response and reload', async ({
  page,
  request,
}) => {
  await page.goto('/?repo=fixture%2Frepository');
  await configure(request, { delayCreate: true });
  await send(page, 'Task A');
  await expect.poll(async () => (await state(request)).pending).toContain('create');
  await page.getByRole('link', { name: 'Source conversation', exact: true }).click();
  await expect(page).toHaveURL(/\/source$/);
  await newTask(page);
  await configure(request, { dropCreate: true });
  await send(page, 'Task B');
  await expect.poll(async () => (await state(request)).creates.length).toBe(2);
  await expect(page.locator('#new-task-prompt')).toBeEnabled();
  const response = page.waitForResponse(
    (reply) => reply.url().endsWith('/v1/agent/jobs') && reply.ok(),
  );
  await release(request, 'create');
  await response;
  await expect(page.locator('#new-task-prompt')).toHaveValue('Task B');
  await page.reload();
  await expect(page.locator('#new-task-prompt')).toHaveValue('Task B');
  await page.locator('#new-task-prompt').press('Enter');
  await expect(page).toHaveURL(/\/created-2$/);
  expect((await state(request)).creates).toHaveLength(2);
});

test('going offline keeps the draft and retry creates exactly one task after reconnecting', async ({
  page,
  context,
  request,
}) => {
  await page.goto('/?repo=fixture%2Frepository');
  await expect(page.locator('#new-task-prompt')).toBeEnabled();
  await context.setOffline(true);
  await send(page, 'Offline draft');
  await expect(page.getByText(/network|fetch|connection/i).last()).toBeVisible();
  expect((await state(request)).creates).toHaveLength(0);
  await expect(page.locator('#new-task-prompt')).toHaveValue('Offline draft');
  await context.setOffline(false);
  await page.locator('#new-task-prompt').press('Enter');
  await expect(page).toHaveURL(/\/created-1$/);
  expect((await state(request)).creates).toHaveLength(1);
});

test('a failed terminal chunk download can be retried without reloading the document', async ({
  page,
  request,
}) => {
  await page.goto('/source');
  await page.getByRole('button', { name: 'Open workspace', exact: true }).click();
  // These libraries are lazy: reject their first real chunk request only.
  let failed = false;
  await page.route('**/_next/static/chunks/*.js', async (route) => {
    const response = await route.fetch();
    const text = await response.text();
    if (!failed && text.includes('xterm-screen')) {
      failed = true;
      await route.abort('internetdisconnected');
    } else await route.fulfill({ response });
  });
  await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
  await expect.poll(() => failed).toBe(true);
  await page.getByRole('button', { name: 'New terminal', exact: true }).click();
  await expect(page.locator('.xterm-screen')).toBeVisible();
  await expect.poll(async () => (await state(request)).terminalStreams).toBeGreaterThan(0);
});
