import { type Locator, type Page } from '@playwright/test';
import { expect, test } from './network-change';

// The signed-out home page's demo is drawn tilted in 3D, so only a real
// browser can say whether a click on one of its tabs reaches that tab.
// Chromium once painted the window flat but hit-tested it in perspective:
// a click on Files landed on the conversation, and Files stayed unselected.

const OPTIONS = {
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
};

test.beforeEach(async ({ request, page }) => {
  await request.post('/__fixture/reset');
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Not signed in' } }),
  );
  await page.route('**/api/v1/auth/options', (route) => route.fulfill({ json: OPTIONS }));
  await page.route('**/api/v1/auth/config', (route) =>
    route.fulfill({ json: { signup_enabled: true } }),
  );
});

/** Whether the point a pointer would press, the tab's visible centre, hits the tab itself. */
async function hitsItself(tab: Locator) {
  return tab.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    const hit = document.elementFromPoint(x, y);
    return x >= 0 && x <= window.innerWidth && (hit === element || element.contains(hit));
  });
}

/** Hold the demo on its finished first task: chosen by hand, it does not move on. */
async function openFinishedDemo(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: /Review the result/ }).click();
  await expect(page.locator('[data-demo="pr-row"]')).toHaveAttribute('data-state', 'opened');
  return page.getByRole('group', { name: /Cloud Agent workspace/ });
}

for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  for (const width of [1101, 1280, 1440, 1920]) {
    test.describe(`the 3D demo at ${width}px, motion ${reducedMotion}`, () => {
      test.use({ viewport: { width, height: 900 }, reducedMotion });

      test('a normal click selects each workspace view', async ({ page }) => {
        const demo = await openFinishedDemo(page);
        const window = demo.locator('[data-demo="window"]');
        await expect(window).toBeInViewport({ ratio: 0.99 });
        const views = demo.getByRole('tablist', { name: 'Workspace views' });
        for (const name of ['Files', 'Terminal', 'Changes']) {
          const tab = views.getByRole('tab', { name });
          await expect.poll(() => hitsItself(tab)).toBe(true);
          await tab.click();
          await expect(tab).toHaveAttribute('aria-selected', 'true');
          await expect(demo.getByRole('tabpanel')).toHaveAccessibleName(name);
        }
      });
    });
  }
}

test.describe('the demo from the keyboard', () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test('tabs into the views and selects them with the arrow keys', async ({ page }, info) => {
    const demo = await openFinishedDemo(page);
    const views = demo.getByRole('tablist', { name: 'Workspace views' });
    const tab = (name: string) => views.getByRole('tab', { name });

    // The replay button comes just before the demo; one Tab reaches the selected view.
    await page.getByRole('button', { name: 'See it work' }).focus();
    await page.keyboard.press('Tab');
    await expect(tab('Changes')).toBeFocused();
    await expect(tab('Changes')).toHaveAttribute('aria-selected', 'true');
    await expect(tab('Changes')).toBeInViewport({ ratio: 1 });

    await page.keyboard.press('ArrowRight');
    await expect(tab('Files')).toBeFocused();
    await expect(tab('Files')).toHaveAttribute('aria-selected', 'true');
    await expect(demo.getByRole('tabpanel')).toHaveAccessibleName('Files');
    await page.keyboard.press('ArrowRight');
    await expect(tab('Terminal')).toBeFocused();
    await expect(tab('Terminal')).toBeInViewport({ ratio: 1 });
    await page.keyboard.press('Home');
    await expect(tab('Changes')).toBeFocused();
    await page.keyboard.press('ArrowLeft');
    await expect(tab('Terminal')).toHaveAttribute('aria-selected', 'true');
    await expect(demo.getByRole('tabpanel')).toHaveAccessibleName('Terminal');
    await page.screenshot({ path: info.outputPath('demo-keyboard.png') });

    // The list is one stop; the next Tab reaches the selected view's panel.
    await page.keyboard.press('Tab');
    await expect(demo.getByRole('tabpanel', { name: 'Terminal' })).toBeFocused();
  });
});

const LOCALES = [
  { locale: 'en', signUp: 'Sign up', signIn: 'Sign in' },
  { locale: 'zh-CN', signUp: '注册', signIn: '登录' },
] as const;

for (const width of [320, 375, 390]) {
  for (const { locale, signUp, signIn } of LOCALES) {
    test.describe(`the signed-out header on a ${width}px phone in ${locale}`, () => {
      test.use({ viewport: { width, height: 740 }, isMobile: true, hasTouch: true });

      test('keeps its account links on screen, with no language or theme switch', async ({
        page,
      }) => {
        await page.addInitScript((value) => {
          window.localStorage.setItem('agents.locale', value);
        }, locale);
        await page.goto('/');
        const nav = page.locator('header nav');
        const up = nav.getByRole('link', { name: signUp, exact: true });
        await expect(up).toBeVisible();
        await expect(nav.getByRole('link', { name: signIn, exact: true })).toBeVisible();
        const box = await up.boundingBox();
        expect(box && box.x + box.width).toBeLessThanOrEqual(width);
        const product = await page.locator('[data-demo="window"]').boundingBox();
        // The hero shows a substantial part of the product before visitors have to scroll.
        expect(product && product.y).toBeLessThan(500);
        // The header holds only the brand and the account links.
        await expect(page.locator('header').getByRole('button')).toHaveCount(0);

        for (const path of ['/login', '/signup', '/forgot-password']) {
          await page.goto(path);
          await expect(page.locator('main form, main [role="status"]').first()).toBeVisible();
          await expect(page.locator('header').getByRole('button')).toHaveCount(0);
          await expect
            .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
            .toBeLessThanOrEqual(width);
        }
      });
    });
  }
}
