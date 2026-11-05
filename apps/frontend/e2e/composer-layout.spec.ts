import { expect, test, type Page } from '@playwright/test';

// The new-task composer's two rows of controls under the task box: where the
// task runs, then how it runs and Run. Long names must wrap the rows by the
// composer's own width rather than scroll sideways or slide under Run, which
// once left the model clipped and reasoning out of sight.

const connection = `mconn_${'e'.repeat(32)}`;
const repo = 'an-organization-with-a-long-name/a-repository-name-long-enough-to-truncate';
const branch = 'feature/a-branch-name-long-enough-to-need-truncation';
const model = 'provider-model-with-a-very-long-identifier-2026-11-04';

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.route('**/api/v1/agent/config', (route) =>
    route.fulfill({
      json: {
        repos: [repo, 'fixture/repository'],
        runtimes: ['claude-code', 'codex'],
        default_model: model,
        default_model_connection_id: connection,
        model_connections_enabled: true,
        model_connections: [
          {
            id: connection,
            name: 'Fixture provider',
            models: [model],
            model_runtimes: { [model]: ['claude-code', 'codex'] },
          },
        ],
        github_connected: true,
        github_install_url: null,
      },
    }),
  );
  await page.route('**/api/v1/agent/branches*', (route) =>
    route.fulfill({ json: { branches: [branch, 'dev'], default: branch } }),
  );
});

test.afterEach(async ({ request }) => {
  const state = await (await request.get('/__fixture/state')).json();
  expect(state.unknown).toEqual([]);
  expect(state.creates).toEqual([]);
});

/** Every visible composer control, as boxes, with the composer's own box. */
async function controls(page: Page) {
  return page.locator('.ui-composer').evaluate((composer) => {
    const toolbar = composer.lastElementChild as HTMLElement;
    const boxes = [...toolbar.querySelectorAll<HTMLButtonElement>('button')]
      .filter((button) => button.getClientRects().length > 0)
      .map((button) => {
        const rect = button.getBoundingClientRect();
        return {
          name: button.getAttribute('aria-label') ?? button.textContent?.trim() ?? '',
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
        };
      });
    const rect = composer.getBoundingClientRect();
    const overflowing = [toolbar, ...toolbar.querySelectorAll<HTMLElement>('*')].filter(
      (element) => element.scrollWidth > element.clientWidth + 1 && !element.matches('span'),
    ).length;
    return { composer: { left: rect.left, right: rect.right }, boxes, overflowing };
  });
}

async function expectLaidOut(page: Page) {
  const { composer, boxes, overflowing } = await controls(page);
  expect(overflowing).toBe(0);
  for (const box of boxes) {
    expect(box.left, box.name).toBeGreaterThanOrEqual(composer.left);
    expect(box.right, box.name).toBeLessThanOrEqual(composer.right);
  }
  for (const [index, a] of boxes.entries()) {
    for (const b of boxes.slice(index + 1)) {
      const overlap =
        Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 &&
        Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
      expect(overlap, `${a.name} overlaps ${b.name}`).toBe(false);
    }
  }
  const find = (pattern: RegExp) => boxes.find((box) => pattern.test(box.name))!;
  // Where first, then how: the repository row sits above the run row.
  expect(find(/^Repository:/).bottom).toBeLessThanOrEqual(find(/^Agent$/).top);
  expect(find(/^Branch$/).bottom).toBeLessThanOrEqual(find(/^Model$/).top);
}

for (const width of [1280, 768, 390, 320]) {
  test(`composer controls wrap without overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(`/?repo=${encodeURIComponent(repo)}`);
    const repository = page.getByRole('button', { name: `Repository: ${repo}`, exact: true });
    // The name is shown; the owner stays in the accessible name and title.
    await expect(repository).toHaveText('a-repository-name-long-enough-to-truncate');
    await expect(repository).toHaveAttribute('title', repo);
    await expect(page.getByRole('button', { name: 'Branch', exact: true })).toHaveAttribute(
      'value',
      branch,
    );
    const agent = page.getByRole('button', { name: 'Agent', exact: true });
    await expect(agent).toHaveText('Claude Code');
    await expect(agent).toHaveAttribute('value', 'claude-code');
    await expect(page.getByRole('button', { name: 'Model', exact: true })).toHaveAttribute(
      'value',
      model,
    );
    await expectLaidOut(page);
    await expect(page.getByRole('button', { name: /^Run/ })).toBeInViewport({ ratio: 1 });
  });
}

test('a composer narrower than the viewport wraps by its own width', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto(`/?repo=${encodeURIComponent(repo)}`);
  await expect(page.getByRole('button', { name: 'Branch', exact: true })).toHaveAttribute(
    'value',
    branch,
  );
  // No viewport breakpoint applies at this width; only the box itself is narrow.
  await page.locator('.ui-composer').evaluate((composer) => {
    (composer as HTMLElement).style.width = '280px';
  });
  await expectLaidOut(page);

  // The pickers still open from the keyboard and hand focus back.
  const agent = page.getByRole('button', { name: 'Agent', exact: true });
  await agent.focus();
  await page.keyboard.press('ArrowDown');
  const panel = page.getByRole('dialog', { name: 'Agent', exact: true });
  await expect(panel.getByRole('option', { name: 'Codex', exact: true })).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(panel).toHaveCount(0);
  await expect(agent).toBeFocused();
  await expect(agent).toHaveAttribute('value', 'codex');
  await expectLaidOut(page);
});

test('the temporary workspace row has no branch', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  // A temporary workspace reads the account's global environment, not a repository's.
  await page.route('**/api/v1/agent/environment-globals', (route) =>
    route.fulfill({
      json: {
        id: null,
        name: 'Global',
        repo: null,
        revision: 0,
        variables: [],
        setup_script: '',
        start_script: '',
        verify_script: '',
        actions: [],
      },
    }),
  );
  await page.goto(`/?repo=${encodeURIComponent(':temporary')}`);
  await expect(
    page.getByRole('button', { name: 'Repository: Start from scratch', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Branch', exact: true })).toHaveCount(0);
  const { composer, boxes, overflowing } = await controls(page);
  expect(overflowing).toBe(0);
  for (const box of boxes) expect(box.right, box.name).toBeLessThanOrEqual(composer.right);
});
