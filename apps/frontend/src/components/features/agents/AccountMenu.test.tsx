// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { ThemeProvider } from '@/components/providers/ThemeProvider';
import { NEUTRAL_SITE_CONFIG, type SiteBranding } from '@/config/site';
import { announceProfileSaved } from '@/lib/api/profileEvents';
import { getUserProfile, type UserProfile } from '@/lib/api/userProfile';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { DARK_CLASS, THEME_STORAGE_KEY } from '@/lib/theme';
import { AccountMenu } from './AccountMenu';

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
const logout = vi.hoisted(() => vi.fn());
let pathname = '/';

vi.mock('next/navigation', () => ({
  useRouter: () => router,
  usePathname: () => pathname,
}));

vi.mock('react-hot-toast', () => ({ default: { error: vi.fn() } }));

vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn() }));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({ state: { user: { id: 'ada' } }, logout }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});

const PROFILE: UserProfile = {
  email: 'ada@example.com',
  first_name: 'Ada',
  last_name: 'Lovelace',
  image_data_url: null,
  revision: 1,
  companion_data_url: 'data:image/svg+xml,<svg id="ada-companion"></svg>',
};

beforeEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove(DARK_CLASS);
  document.documentElement.lang = 'en';
  pathname = '/';
  router.replace.mockReset();
  logout.mockReset();
  vi.mocked(toast.error).mockReset();
  vi.mocked(getUserProfile).mockReset().mockResolvedValue(PROFILE);
  vi.stubGlobal(
    'matchMedia',
    vi.fn((media: string) => ({
      matches: false,
      media,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove(DARK_CLASS);
  document.documentElement.lang = 'en';
});

function renderMenu(
  branding: Partial<SiteBranding> = {},
  variant: 'sidebar' | 'header' = 'sidebar',
) {
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <SiteConfigProvider
          config={{
            ...NEUTRAL_SITE_CONFIG,
            branding: { ...NEUTRAL_SITE_CONFIG.branding, ...branding },
          }}
        >
          <AccountMenu variant={variant} />
        </SiteConfigProvider>
      </LocaleProvider>
    </ThemeProvider>,
  );
}

async function openMenu() {
  const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
  fireEvent.click(trigger);
  return screen.getByRole('menu', { name: 'Account menu' });
}

function submenu(name: string) {
  fireEvent.click(screen.getByRole('menuitem', { name }));
  return screen.getByRole('menu', { name });
}

describe('account menu', () => {
  it("shows who is signed in, with the account's fixed companion until a photo is uploaded", async () => {
    renderMenu();
    const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    expect(trigger).toHaveTextContent('Ada Lovelace');
    expect(trigger.querySelector('img')).toHaveAttribute('src', PROFILE.companion_data_url);
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('shows a placeholder, not a stale picture, until the profile answers', () => {
    vi.mocked(getUserProfile).mockReturnValue(new Promise(() => {}));
    renderMenu();
    const trigger = screen.getByRole('button', { name: /^Account menu/ });
    expect(trigger.querySelector('img')).toBeNull();
    expect(trigger).toHaveTextContent('·');
  });

  it('uses the email when no name is set, and the uploaded photo when there is one', async () => {
    vi.mocked(getUserProfile).mockResolvedValue({
      ...PROFILE,
      first_name: '',
      last_name: '',
      image_data_url: 'data:image/png;base64,AAAA',
    });
    renderMenu();
    const trigger = await screen.findByRole('button', { name: 'Account menu: ada@example.com' });
    expect(trigger.querySelector('img')).toHaveAttribute('src', 'data:image/png;base64,AAAA');
  });

  it('shows a name saved elsewhere in the tab without reloading', async () => {
    renderMenu();
    await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    act(() => announceProfileSaved({ ...PROFILE, first_name: 'Augusta', revision: 2 }));
    expect(
      screen.getByRole('button', { name: 'Account menu: Augusta Lovelace' }),
    ).toBeInTheDocument();
    expect(getUserProfile).toHaveBeenCalledTimes(1);
  });

  it('holds the account pages, preferences, help and sign-out', async () => {
    pathname = '/settings/my-settings';
    renderMenu({ gatewayUrl: 'https://gateway.example', docsUrl: 'https://docs.example/agents' });
    const menu = await openMenu();

    expect(within(menu).getByText('ada@example.com')).toBeInTheDocument();
    expect(
      within(menu)
        .getAllByRole('menuitem')
        .map((item) => item.textContent),
    ).toEqual([
      'Dashboard',
      'My Settings',
      'Appearance',
      'Language',
      'Help',
      'Switch account',
      'Log out',
    ]);
    expect(within(menu).getByRole('menuitem', { name: 'Dashboard' })).toHaveAttribute(
      'href',
      'https://gateway.example/dashboard',
    );
    const settings = within(menu).getByRole('menuitem', { name: 'My Settings' });
    expect(settings).toHaveAttribute('href', '/settings/my-settings');
    expect(settings).toHaveAttribute('aria-current', 'page');
    expect(document.activeElement).toBe(within(menu).getByRole('menuitem', { name: 'Dashboard' }));
  });

  it('switches account through the gateway’s chooser, back to this page', async () => {
    pathname = '/ajob_123';
    renderMenu();
    const menu = await openMenu();

    const item = within(menu).getByRole('menuitem', { name: 'Switch account' });
    const url = new URL(item.getAttribute('href') ?? '', 'https://agents.example');

    expect(url.pathname).toBe('/v1/session/login');
    expect(url.searchParams.get('prompt')).toBe('select_account');
    expect(url.searchParams.get('return_to')).toBe('/ajob_123');
  });

  it('leaves out Dashboard without a gateway and Help without docs or contact', async () => {
    renderMenu();
    const menu = await openMenu();
    expect(
      within(menu)
        .getAllByRole('menuitem')
        .map((item) => item.textContent),
    ).toEqual(['My Settings', 'Appearance', 'Language', 'Switch account', 'Log out']);
    expect(menu.querySelector('a[href=""]')).toBeNull();
  });

  it('switches the theme from Appearance and remembers it', async () => {
    renderMenu();
    await openMenu();
    const appearance = submenu('Appearance');
    expect(within(appearance).getByRole('menuitemradio', { name: 'System' })).toHaveAttribute(
      'aria-checked',
      'true',
    );

    fireEvent.click(within(appearance).getByRole('menuitemradio', { name: 'Dark' }));
    expect(document.documentElement).toHaveClass(DARK_CLASS);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
    expect(within(appearance).getByRole('menuitemradio', { name: 'Dark' })).toHaveAttribute(
      'aria-checked',
      'true',
    );

    fireEvent.click(within(appearance).getByRole('menuitemradio', { name: 'Light' }));
    expect(document.documentElement).not.toHaveClass(DARK_CLASS);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
  });

  it('switches the display language from Language and retranslates the menu', async () => {
    renderMenu();
    await openMenu();
    const language = submenu('Language');
    fireEvent.click(within(language).getByRole('menuitemradio', { name: '简体中文' }));

    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh-CN');
    expect(document.documentElement.lang).toBe('zh-Hans');
    const menu = screen.getByRole('menu', { name: '账号菜单' });
    expect(within(menu).getByRole('menuitem', { name: '退出登录' })).toBeInTheDocument();
  });

  it('opens the docs in a new tab and a mail contact in the mail client', async () => {
    renderMenu({
      appName: 'Acme Cloud Agent',
      orgName: 'Acme',
      docsUrl: 'https://docs.acme.example/agents',
      contactUrl: 'mailto:help@acme.example',
    });
    await openMenu();
    const help = submenu('Help');

    const docs = within(help).getByRole('menuitem', { name: 'Acme Docs' });
    expect(docs).toHaveAttribute('href', 'https://docs.acme.example/agents');
    expect(docs).toHaveAttribute('target', '_blank');
    expect(docs).toHaveAttribute('rel', 'noopener noreferrer');

    const contact = within(help).getByRole('menuitem', { name: 'Contact us' });
    expect(contact).toHaveAttribute('href', 'mailto:help@acme.example');
    expect(contact).not.toHaveAttribute('target');
  });

  it('names the docs after the app without an organization, and opens a contact page in a tab', async () => {
    renderMenu({
      appName: 'Acme Cloud Agent',
      contactUrl: 'https://acme.example/support',
    });
    await openMenu();
    const help = submenu('Help');

    expect(within(help).queryByRole('menuitem', { name: /Docs/ })).not.toBeInTheDocument();
    expect(within(help).getByRole('menuitem', { name: 'Contact us' })).toHaveAttribute(
      'target',
      '_blank',
    );

    cleanup();
    renderMenu({ appName: 'Acme Cloud Agent', docsUrl: 'https://docs.acme.example/' });
    await openMenu();
    expect(
      within(submenu('Help')).getByRole('menuitem', { name: 'Acme Cloud Agent Docs' }),
    ).toBeInTheDocument();
  });

  it('opens centered on the row and fits a long docs name in the Help submenu', async () => {
    vi.stubGlobal('innerWidth', 1200);
    vi.stubGlobal('innerHeight', 800);
    renderMenu({ appName: 'FreeInference Cloud Agent', docsUrl: 'https://docs.example/' });
    const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    trigger.getBoundingClientRect = () =>
      ({ left: 0, right: 300, width: 300, top: 740, bottom: 780, height: 40 }) as DOMRect;
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu', { name: 'Account menu' });
    // (300 - 248) / 2: centered over the row, not pinned to its left edge.
    expect(menu).toHaveStyle({ left: '26px' });

    const help = submenu('Help');
    expect(help).toHaveStyle({ width: 'max-content', minWidth: '216px', maxWidth: '360px' });
    expect(
      within(help).getByRole('menuitem', { name: 'FreeInference Cloud Agent Docs' }),
    ).toBeInTheDocument();
  });

  it('keeps a centered menu on screen next to a trigger at the edge', async () => {
    vi.stubGlobal('innerWidth', 1200);
    vi.stubGlobal('innerHeight', 800);
    renderMenu({}, 'header');
    const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    trigger.getBoundingClientRect = () =>
      ({ left: 1160, right: 1192, width: 32, top: 8, bottom: 40, height: 32 }) as DOMRect;
    fireEvent.click(trigger);
    // 1200 - 248 - 8: the right edge stops at the margin.
    expect(screen.getByRole('menu', { name: 'Account menu' })).toHaveStyle({ left: '944px' });
  });

  it('works from the keyboard: arrows, into a submenu and back, Escape', async () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    renderMenu();
    const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const menu = screen.getByRole('menu', { name: 'Account menu' });
    expect(document.activeElement).toBe(
      within(menu).getByRole('menuitem', { name: 'My Settings' }),
    );

    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    const appearance = within(menu).getByRole('menuitem', { name: 'Appearance' });
    expect(document.activeElement).toBe(appearance);

    fireEvent.keyDown(appearance, { key: 'ArrowRight' });
    const panel = screen.getByRole('menu', { name: 'Appearance' });
    expect(appearance).toHaveAttribute('aria-expanded', 'true');
    expect(document.activeElement).toBe(within(panel).getByRole('menuitemradio', { name: 'Dark' }));

    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(screen.queryByRole('menu', { name: 'Appearance' })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(appearance);

    fireEvent.keyDown(document.activeElement!, { key: 'End' });
    expect(document.activeElement).toBe(within(menu).getByRole('menuitem', { name: 'Log out' }));

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it('moves focus into a submenu clicked after the pointer opened it, and no further', async () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    renderMenu();
    await openMenu();
    const menu = screen.getByRole('menu', { name: 'Account menu' });
    const appearance = within(menu).getByRole('menuitem', { name: 'Appearance' });

    fireEvent.pointerEnter(appearance);
    fireEvent.click(appearance);
    const panel = screen.getByRole('menu', { name: 'Appearance' });
    expect(document.activeElement).toBe(within(panel).getByRole('menuitemradio', { name: 'Dark' }));

    // Passing over another parent opens it without taking the focus.
    const settings = within(menu).getByRole('menuitem', { name: 'My Settings' });
    settings.focus();
    fireEvent.pointerEnter(within(menu).getByRole('menuitem', { name: 'Language' }));
    expect(screen.getByRole('menu', { name: 'Language' })).toBeInTheDocument();
    expect(document.activeElement).toBe(settings);
  });

  it('closes on a click outside', async () => {
    renderMenu();
    await openMenu();
    submenu('Appearance');
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('is the avatar alone in the header', async () => {
    renderMenu({}, 'header');
    const trigger = await screen.findByRole('button', { name: 'Account menu: Ada Lovelace' });
    expect(trigger).not.toHaveTextContent('Ada Lovelace');
  });
});

describe('account menu sign-out', () => {
  it('leaves for the start page once the session has ended', async () => {
    logout.mockResolvedValueOnce(undefined);
    renderMenu();
    const menu = await openMenu();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Log out' }));
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/'));
    expect(toast.error).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  // The server kept the session, so this browser is still signed in. Leaving
  // the page as though it were not is what told the user they were done.
  it('says the session is still open when signing out failed', async () => {
    logout.mockRejectedValueOnce(new Error('HTTP 502: Bad Gateway'));
    renderMenu();
    const menu = await openMenu();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Log out' }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Could not log out. You are still signed in; try again.',
      ),
    );
    expect(router.replace).not.toHaveBeenCalled();
  });
});
