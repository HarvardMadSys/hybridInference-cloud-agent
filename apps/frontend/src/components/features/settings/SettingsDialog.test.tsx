// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FirstRunProvider } from '@/components/features/agents/firstRun';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { ThemeProvider } from '@/components/providers/ThemeProvider';
import {
  getAgentConfig,
  listAgentConversations,
  type AgentConversationApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { DARK_CLASS, THEME_STORAGE_KEY } from '@/lib/theme';
import { SettingsLauncher } from './SettingsLauncher';

vi.mock('next/navigation', () => ({ usePathname: () => '/' }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: { id: 'alice' } } }),
}));

vi.mock('@/lib/api/agents', () => ({
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
}));

function renderSettings() {
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <SettingsLauncher />
      </LocaleProvider>
    </ThemeProvider>,
  );
}

const SKIPPED = 'agents.gettingStarted.skipped.alice';

/** Settings inside the app shell, for an owner with this many conversations. */
function renderInApp(conversations: number) {
  vi.mocked(listAgentConversations).mockResolvedValue({
    conversations: Array.from(
      { length: conversations },
      (_, index): AgentConversationApi => ({
        job: { id: `job_${index}`, thread_id: `thread_${index}` } as AgentJobApi,
        title: 'A task',
        archived: false,
        last_activity_at: null,
      }),
    ),
    next_cursor: null,
  });
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: ['claude-code'],
    models: ['model-a'],
    github_connected: false,
    github_install_url: '/agents/integrations',
  });
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <FirstRunProvider userId="alice">
          <SettingsLauncher />
        </FirstRunProvider>
      </LocaleProvider>
    </ThemeProvider>,
  );
}

async function openDialog() {
  fireEvent.click(await screen.findByRole('button', { name: /Settings|设置/ }));
  return screen.findByRole('dialog');
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.className = '';
  document.documentElement.lang = 'en';
});

afterEach(cleanup);

describe('the settings entry', () => {
  it('opens settings when the sidebar row is clicked', async () => {
    renderSettings();
    const trigger = await screen.findByRole('button', { name: /Settings|设置/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(trigger);

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('hands focus back to the row when the dialog closes', async () => {
    renderSettings();
    const trigger = await screen.findByRole('button', { name: /Settings|设置/ });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // Otherwise focus falls to <body> and a keyboard user restarts from the
    // top of the document.
    expect(document.activeElement).toBe(trigger);
  });

  it('closes on Escape', async () => {
    renderSettings();
    const dialog = await openDialog();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('moves focus into the dialog when it opens', async () => {
    renderSettings();
    const dialog = await openDialog();
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  });
});

describe('theme', () => {
  it('paints the document dark and remembers the choice', async () => {
    renderSettings();
    await openDialog();

    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));

    await waitFor(() => expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });

  it('goes back to light, and stores `system` as `system`', async () => {
    renderSettings();
    await openDialog();

    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    await waitFor(() => expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true));

    fireEvent.click(screen.getByRole('radio', { name: 'Light' }));
    await waitFor(() =>
      expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false),
    );
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');

    fireEvent.click(screen.getByRole('radio', { name: 'System' }));
    await waitFor(() => expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('system'));
  });

  it('reopens on the choice that was made', async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    renderSettings();
    await openDialog();
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked());
  });
});

describe('language', () => {
  it('retranslates the interface and updates the document language', async () => {
    renderSettings();
    await openDialog();
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zh-CN' } });

    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: '深色' })).toBeInTheDocument();
    // Not `zh-CN`: the html attribute wants a script-qualified tag.
    await waitFor(() => expect(document.documentElement.lang).toBe('zh-Hans'));
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh-CN');
  });

  it('lists each language under its own name', async () => {
    renderSettings();
    await openDialog();
    // "Chinese (Simplified)" is no help to a reader who cannot read the
    // English around it.
    expect(screen.getByRole('option', { name: '简体中文' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'English' })).toBeInTheDocument();
  });

  it('starts in the stored language', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: /Settings|设置/ }));
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
  });
});

describe('getting started', () => {
  beforeEach(() => vi.clearAllMocks());

  it('is offered back to a newcomer who skipped it', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderInApp(0);
    await openDialog();

    const section = await screen.findByRole('region', { name: 'Getting started' });
    fireEvent.click(within(section).getByRole('button', { name: 'Show again' }));

    // Said in place until the dialog closes, rather than the row vanishing.
    expect(within(section).getByRole('status')).toHaveTextContent('Shown');
    expect(within(section).queryByRole('button', { name: 'Show again' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(SKIPPED)).toBeNull();
  });

  it('keeps focus in the dialog when Show again goes away', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderInApp(0);
    const dialog = await openDialog();

    const show = await screen.findByRole('button', { name: 'Show again' });
    show.focus();
    fireEvent.click(show);

    // Not dropped to <body>, where Escape and Tab no longer reach the dialog.
    expect(screen.getByRole('status')).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
  });

  it('says what it is from a help button', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderInApp(0);
    await openDialog();

    const help = await screen.findByRole('button', { name: 'What is getting started?' });
    expect(help).toHaveAccessibleDescription(
      'Takes you from connecting GitHub to your first task.',
    );
    // Hover and focus open it too; a click is for a screen with no hover.
    expect(help).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(help);
    expect(help).toHaveAttribute('aria-expanded', 'true');
  });

  it.each([
    ['a newcomer who has not skipped it', 0, false],
    ['someone with a conversation, even with Skip remembered', 1, true],
  ])('is not offered to %s', async (_case, conversations, skipped) => {
    if (skipped) window.localStorage.setItem(SKIPPED, '1');
    renderInApp(conversations);
    await openDialog();

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
  });

  it('is not offered outside the app shell', async () => {
    window.localStorage.setItem(SKIPPED, '1');
    renderSettings();
    await openDialog();

    expect(screen.queryByRole('region', { name: 'Getting started' })).not.toBeInTheDocument();
  });
});
