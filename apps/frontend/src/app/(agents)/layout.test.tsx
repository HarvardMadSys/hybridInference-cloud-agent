// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import Link from 'next/link';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { listMcpInvocations, type McpInvocationsApi } from '@/lib/api/mcp';
import type { AgentAccessApi } from '@/lib/api/session';
import { SkillDialog } from '@/components/features/agents/skills/SkillDialog';
import { KeyboardShortcutsSettings } from '@/components/features/settings/KeyboardShortcutsSettings';

let access: AgentAccessApi | undefined;
let role = 'internal';
let userId: string | undefined;
const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
const logout = vi.hoisted(() => vi.fn());

beforeEach(() => {
  access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  role = 'internal';
  userId = undefined;
  pathname = '/';
  sessionStorage.clear();
  window.history.replaceState(null, '', '/');
  vi.mocked(listMcpInvocations).mockReset().mockResolvedValue({ invocations: [] });
  router.replace.mockReset();
  logout.mockReset();
});

let pathname = '/';
let statusUrl = '/status';

vi.mock('@/lib/api/mcp', async (original) => ({
  ...(await original<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => router,
  usePathname: () => pathname,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/features/agents/firstRun', () => ({
  FirstRunProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('@/components/features/agents/agentPush', async (original) => ({
  ...(await original<typeof import('@/components/features/agents/agentPush')>()),
  useAgentChangeStream: vi.fn(),
}));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({
      state: {
        user: {
          id: userId,
          user_name: 'Ada',
          email: 'ada@example.com',
          role,
          agent_access: access,
        },
      },
      logout,
    }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});

vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { appName: 'FreeInference', orgName: '', orgUrl: '', statusUrl },
    features: { rag: false },
  }),
}));

// The menu has its own tests; here it only matters where it is rendered.
vi.mock('@/components/features/agents/AccountMenu', () => ({
  AccountMenu: ({ variant }: { variant: string }) => (
    <button type="button" data-testid={`account-menu-${variant}`}>
      Account menu
    </button>
  ),
}));

vi.mock('@/components/features/agents/AgentsSidebar', () => ({
  AgentsSidebar: ({
    collapsed,
    headerAction,
    settingsNavigation,
  }: {
    collapsed?: boolean;
    headerAction?: React.ReactNode;
    settingsNavigation?: { backHref: string };
  }) =>
    collapsed ? null : settingsNavigation ? (
      <aside id="agents-sidebar" data-testid="settings-sidebar-stub">
        <Link href={settingsNavigation.backHref} onClick={(event) => event.preventDefault()}>
          Back to chats
        </Link>
        {headerAction}
        <Link href="/settings/plugins" onClick={(event) => event.preventDefault()}>
          Plugins &amp; MCPs
        </Link>
      </aside>
    ) : (
      <aside id="agents-sidebar" data-testid="sidebar-stub">
        tasks
        {headerAction}
        <Link href="/" onClick={(event) => event.preventDefault()}>
          New task
        </Link>
        <Link href="/job-1" onClick={(event) => event.preventDefault()}>
          Existing task
        </Link>
        <input
          aria-label="Task search"
          onKeyDown={(event) => {
            if (event.key === 'Escape') event.preventDefault();
          }}
        />
      </aside>
    ),
}));

import AgentsLayout from './layout';
import { SIDEBAR_COLLAPSED_STORAGE_KEY } from './sidebarStorage';

function toggle(): HTMLElement {
  return screen.getByRole('button', { name: /(Collapse|Expand) task list/ });
}

describe('AgentsLayout sidebar collapse', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => cleanup());

  it('keeps the collapse control in the task list and the expand control in the header', () => {
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Collapse task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    expect(toggle()).toHaveAttribute('aria-controls', 'agents-sidebar');
    expect(toggle()).toHaveAttribute('aria-keyshortcuts', 'Meta+B Control+B');
    expect(screen.getByTestId('sidebar-stub')).toContainElement(toggle());

    fireEvent.click(toggle());

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('banner')).toContainElement(toggle());
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('1');

    fireEvent.click(toggle());

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(screen.getByTestId('sidebar-stub')).toContainElement(toggle());
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('0');
  });

  it('starts collapsed when that is how it was left', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, '1');

    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
  });

  it('toggles and persists the task list with Command+B or Control+B', () => {
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(fireEvent.keyDown(window, { key: 'b', metaKey: true })).toBe(false);
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('1');

    expect(fireEvent.keyDown(window, { key: 'b', ctrlKey: true })).toBe(false);
    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('0');
  });

  it.each([
    ['/', 'sidebar-stub', /(Collapse|Expand) task list/],
    ['/settings/my-settings', 'settings-sidebar-stub', /(Collapse|Expand) settings navigation/],
  ])(
    'applies shortcut changes immediately on %s and keeps the mouse toggle after disabling',
    (route, sidebar, name) => {
      pathname = route;
      const sidebarToggle = () => screen.getByRole('button', { name });
      userId = 'layout-keyboard-settings-user';
      render(
        <AgentsLayout>
          <KeyboardShortcutsSettings />
        </AgentsLayout>,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Change shortcut for Toggle task list' }));
      const input = screen.getByRole('textbox', { name: 'New shortcut for Toggle task list' });
      fireEvent.keyDown(input, { key: 'B', ctrlKey: true, shiftKey: true });
      expect(sidebarToggle()).toHaveAttribute('aria-keyshortcuts', 'Meta+Shift+B Control+Shift+B');
      expect(fireEvent.keyDown(window, { key: 'b', ctrlKey: true })).toBe(true);
      expect(screen.getByTestId(sidebar)).toBeInTheDocument();
      fireEvent.keyDown(window, { key: 'B', metaKey: true, shiftKey: true });
      expect(screen.queryByTestId(sidebar)).not.toBeInTheDocument();
      fireEvent.click(
        screen.getByRole('button', { name: 'Disable shortcut for Toggle task list' }),
      );
      expect(sidebarToggle()).not.toHaveAttribute('aria-keyshortcuts');
      expect(fireEvent.keyDown(window, { key: 'B', ctrlKey: true, shiftKey: true })).toBe(true);
      expect(screen.queryByTestId(sidebar)).not.toBeInTheDocument();
      fireEvent.click(sidebarToggle());
      expect(screen.getByTestId(sidebar)).toBeInTheDocument();
    },
  );

  it('ignores repeated or additionally modified shortcut variants', () => {
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );

    expect(fireEvent.keyDown(window, { key: 'b', metaKey: true, repeat: true })).toBe(false);
    expect(fireEvent.keyDown(window, { key: 'b', metaKey: true, shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'b', ctrlKey: true, altKey: true })).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'b' })).toBe(true);

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();
  });
});

describe('AgentsLayout mobile task list', () => {
  let query: { matches: boolean };
  let changeListeners: Set<() => void>;

  beforeEach(() => {
    localStorage.clear();
    pathname = '/';
    query = { matches: true };
    changeListeners = new Set();
    vi.stubGlobal(
      'matchMedia',
      vi.fn((media: string) => ({
        get matches() {
          return query.matches;
        },
        media,
        addEventListener: (_event: string, listener: () => void) => changeListeners.add(listener),
        removeEventListener: (_event: string, listener: () => void) =>
          changeListeners.delete(listener),
      })),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  function renderLayout() {
    return render(
      <AgentsLayout>
        <p>Task composer</p>
      </AgentsLayout>,
    );
  }

  function resizeToMobile(mobile: boolean) {
    act(() => {
      query.matches = mobile;
      changeListeners.forEach((listener) => listener());
    });
  }

  it('opens settings navigation on mobile and closes it after a section is selected', async () => {
    pathname = '/settings/my-settings';
    renderLayout();
    expect(screen.queryByTestId('settings-sidebar-stub')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Expand settings navigation' }));
    expect(screen.getByTestId('settings-sidebar-stub')).toBeVisible();
    expect(screen.getByRole('main', { hidden: true })).not.toBeVisible();
    await waitFor(() => expect(screen.getByRole('link', { name: 'Back to chats' })).toHaveFocus());
    fireEvent.click(screen.getByRole('link', { name: 'Plugins & MCPs' }));
    expect(screen.queryByTestId('settings-sidebar-stub')).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toBeVisible();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();
  });

  it('keeps the account menu in the header only while the mobile task list is closed', () => {
    renderLayout();
    const header = screen.getByRole('banner');
    expect(header).toContainElement(screen.getByTestId('account-menu-header'));

    // Open, the list carries its own account row at the bottom.
    fireEvent.click(screen.getByRole('button', { name: 'Expand task list' }));
    expect(screen.queryByTestId('account-menu-header')).not.toBeInTheDocument();
  });

  it('skips the task list and focuses the workspace when the mobile list is open', async () => {
    renderLayout();
    fireEvent.click(screen.getByRole('button', { name: 'Expand task list' }));
    const workspace = document.getElementById('agents-workspace')!;
    expect(workspace).toHaveAttribute('hidden');

    fireEvent.click(screen.getByRole('link', { name: 'Skip to workspace' }));

    expect(workspace).not.toHaveAttribute('hidden');
    await waitFor(() => expect(workspace).toHaveFocus());
  });

  it('starts with the composer visible and the task list closed on a narrow screen', () => {
    renderLayout();

    expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 767px)');
    expect(screen.getByRole('main')).toBeVisible();
    expect(screen.getByText('Task composer')).toBeVisible();
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();
  });

  it.each(['0', '1'])(
    'opens and closes a task-list view while preserving desktop preference %s',
    (preference) => {
      localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, preference);
      renderLayout();
      const workspace = screen.getByRole('main');

      fireEvent.click(toggle());

      expect(screen.getByTestId('sidebar-stub')).toBeVisible();
      expect(workspace).not.toBeVisible();
      expect(workspace).toHaveAttribute('hidden');
      expect(screen.queryByRole('main')).not.toBeInTheDocument();
      expect(toggle()).toHaveAccessibleName('Collapse task list');
      expect(toggle()).toHaveAttribute('aria-expanded', 'true');
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe(preference);

      fireEvent.click(toggle());

      expect(workspace).toBeVisible();
      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe(preference);
    },
  );

  it('closes the task list with Escape and returns focus to its toggle', () => {
    renderLayout();
    fireEvent.click(toggle());
    screen.getByRole('link', { name: 'Existing task' }).focus();

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toBeVisible();
    expect(toggle()).toHaveFocus();
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
  });

  it('moves focus into the task list when opened with Control+B and back when closed', async () => {
    renderLayout();
    const workspace = screen.getByRole('main');
    workspace.focus();
    expect(workspace).toHaveFocus();

    fireEvent.keyDown(workspace, { key: 'b', ctrlKey: true });

    const firstTaskLink = screen.getByRole('link', { name: 'New task' });
    expect(firstTaskLink).toHaveFocus();
    expect(workspace).not.toBeVisible();

    fireEvent.keyDown(firstTaskLink, { key: 'b', ctrlKey: true });

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(workspace).toBeVisible();
    await waitFor(() => expect(workspace).toHaveFocus());
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();
  });

  it.each([{ ctrlKey: true }, { metaKey: true }])(
    'keeps focus in a portaled Skills modal when the sidebar shortcut uses %j',
    (modifier) => {
      function Workspace() {
        const [open, setOpen] = useState(true);
        return (
          <>
            <p>Task composer</p>
            {open && (
              <SkillDialog title="Skills" onClose={() => setOpen(false)}>
                <input aria-label="Find skills" />
              </SkillDialog>
            )}
          </>
        );
      }
      render(
        <AgentsLayout>
          <Workspace />
        </AgentsLayout>,
      );
      const workspace = screen.getByRole('main');
      const input = screen.getByRole('textbox', { name: 'Find skills' });
      expect(input).toHaveFocus();

      fireEvent.keyDown(input, { key: 'b', ...modifier });

      expect(input).toHaveFocus();
      expect(screen.getByRole('dialog', { name: 'Skills' })).toBeVisible();
      expect(workspace).toBeVisible();
      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-expanded', 'false');
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();

      fireEvent.keyDown(input, { key: 'Tab' });
      expect(screen.getByRole('button', { name: 'Close skill dialog' })).toHaveFocus();
      fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

      workspace.focus();
      fireEvent.keyDown(workspace, { key: 'b', ...modifier });
      expect(screen.getByRole('link', { name: 'New task' })).toHaveFocus();
      expect(workspace).not.toBeVisible();
    },
  );

  it('keeps an open native dialog in control until it closes', () => {
    render(
      <AgentsLayout>
        <dialog open aria-label="Task details">
          <input aria-label="Task title" />
        </dialog>
      </AgentsLayout>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Task details' });
    const input = screen.getByRole('textbox', { name: 'Task title' });
    input.focus();

    fireEvent.keyDown(input, { key: 'b', ctrlKey: true });
    fireEvent.click(toggle());

    expect(input).toHaveFocus();
    expect(screen.getByRole('main')).toBeVisible();
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');

    dialog.removeAttribute('open');
    fireEvent.click(toggle());
    expect(screen.getByTestId('sidebar-stub')).toBeVisible();
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
  });

  it('allows the sidebar shortcut when a modal is hidden', () => {
    render(
      <AgentsLayout>
        <div hidden>
          <div role="dialog" aria-modal="true" aria-label="Inactive dialog" />
        </div>
        <p>Task composer</p>
      </AgentsLayout>,
    );

    fireEvent.keyDown(screen.getByRole('main'), { key: 'b', ctrlKey: true });

    expect(screen.getByTestId('sidebar-stub')).toBeVisible();
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps the task list open when a descendant has handled Escape', () => {
    renderLayout();
    fireEvent.click(toggle());
    const search = screen.getByRole('textbox', { name: 'Task search' });
    search.focus();

    expect(fireEvent.keyDown(search, { key: 'Escape' })).toBe(false);

    expect(screen.getByTestId('sidebar-stub')).toBeVisible();
    expect(screen.queryByRole('main')).not.toBeInTheDocument();
    expect(toggle()).toHaveAttribute('aria-expanded', 'true');
    expect(search).toHaveFocus();
  });

  it.each(['New task', 'Existing task'])(
    'returns to the workspace when selecting %s, including the current route',
    async (linkName) => {
      renderLayout();
      const workspace = screen.getByRole('main');
      fireEvent.click(toggle());

      fireEvent.click(screen.getByRole('link', { name: linkName }));

      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      expect(screen.getByRole('main')).toBeVisible();
      expect(toggle()).toHaveAttribute('aria-expanded', 'false');
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBeNull();
      await waitFor(() => expect(workspace).toHaveFocus());
    },
  );

  it.each(['0', '1'])(
    'restores desktop preference %s when resizing out of an open mobile task list',
    (preference) => {
      localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, preference);
      renderLayout();
      fireEvent.click(toggle());

      resizeToMobile(false);

      expect(screen.getByRole('main')).toBeVisible();
      expect(toggle()).toHaveAttribute('aria-expanded', String(preference === '0'));
      if (preference === '0') expect(screen.getByTestId('sidebar-stub')).toBeVisible();
      else expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe(preference);

      resizeToMobile(true);

      expect(screen.getByRole('main')).toBeVisible();
      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-expanded', 'false');
      expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe(preference);
    },
  );
});

describe('AgentsLayout product navigation', () => {
  beforeEach(() => {
    localStorage.clear();
    pathname = '/';
    statusUrl = '/status';
  });
  afterEach(() => cleanup());

  function nav(): HTMLElement {
    return screen.getByRole('navigation', { name: 'Product navigation' });
  }

  function renderLayout() {
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );
  }

  it('moves the account menu into the header when the task list is collapsed', () => {
    renderLayout();
    expect(screen.queryByTestId('account-menu-header')).not.toBeInTheDocument();

    fireEvent.click(toggle());
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    const account = screen.getByTestId('account-menu-header');
    expect(screen.getByRole('banner')).toContainElement(account);
    expect(nav()).not.toContainElement(account);

    fireEvent.click(toggle());
    expect(screen.queryByTestId('account-menu-header')).not.toBeInTheDocument();
  });

  it('keeps Dashboard, Log out and the website preferences out of the header links', () => {
    renderLayout();
    expect(screen.queryByRole('link', { name: 'Dashboard' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('links Status at the configured status page', () => {
    statusUrl = 'https://status.example.test/';

    renderLayout();

    expect(screen.getByRole('link', { name: 'Status' })).toHaveAttribute(
      'href',
      'https://status.example.test/',
    );
  });

  it('omits Status when no status page is configured', () => {
    // An empty `href` resolves to the current document, so rendering the link
    // anyway gave `target="_blank"` a second tab of the page you were on.
    statusUrl = '';

    renderLayout();

    expect(screen.queryByRole('link', { name: 'Status' })).not.toBeInTheDocument();
    expect(nav().querySelector('a[href=""]')).toBeNull();
  });

  it('marks Agents as the current page inside the task app', () => {
    pathname = '/job-1';

    renderLayout();

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Admin' })).not.toHaveAttribute('aria-current');
  });

  it.each(['/admin/hosts', '/admin/runs', '/admin/runs/job-1'])(
    'moves the highlight to Admin on %s',
    (route) => {
      pathname = route;

      renderLayout();

      expect(screen.getByRole('link', { name: 'Admin' })).toHaveAttribute('aria-current', 'page');

      // And Agents is a way back rather than a label: it was a bare <span>, so
      // the only route off this page was the browser's back button.
      const agents = screen.getByRole('link', { name: 'Agents' });
      expect(agents).not.toHaveAttribute('aria-current');
      expect(agents).toHaveAttribute('href', '/');
    },
  );

  it.each(['/archived'])(
    'keeps the workspace sidebar on %s and only changes the main content',
    (route) => {
      pathname = route;
      renderLayout();
      expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Appearance' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Language' })).not.toBeInTheDocument();

      fireEvent.click(toggle());
      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
      fireEvent.click(toggle());
      expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
    },
  );

  it.each([
    '/integrations',
    '/settings/my-settings',
    '/settings/plugins',
    '/settings/integrations',
    '/settings/environments',
  ])('replaces the task sidebar with collapsible settings navigation on %s', (route) => {
    pathname = route;
    renderLayout();
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(screen.getByTestId('settings-sidebar-stub')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Agents' })).not.toHaveAttribute('aria-current');
    fireEvent.click(screen.getByRole('button', { name: 'Collapse settings navigation' }));
    expect(screen.queryByTestId('settings-sidebar-stub')).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'b', metaKey: true });
    expect(screen.getByTestId('settings-sidebar-stub')).toBeInTheDocument();
  });

  it('remembers a repository query changed on the new-chat route', () => {
    userId = 'alice';
    const view = render(
      <AgentsLayout>
        <p>chat</p>
      </AgentsLayout>,
    );
    window.history.replaceState(null, '', '/?repo=owner%2Frepo');
    view.rerender(
      <AgentsLayout>
        <p>repository chat</p>
      </AgentsLayout>,
    );
    pathname = '/settings/my-settings';
    window.history.replaceState(null, '', pathname);
    view.rerender(
      <AgentsLayout>
        <p>settings</p>
      </AgentsLayout>,
    );
    expect(screen.getByRole('link', { name: 'Back to chats' })).toHaveAttribute(
      'href',
      '/?repo=owner%2Frepo',
    );
  });

  it('returns to the last chat across settings sections and isolates the destination by account', () => {
    userId = 'alice';
    pathname = '/job-1';
    const view = render(
      <AgentsLayout>
        <p>chat</p>
      </AgentsLayout>,
    );
    pathname = '/settings/my-settings';
    view.rerender(
      <AgentsLayout>
        <p>settings</p>
      </AgentsLayout>,
    );
    expect(screen.getByRole('link', { name: 'Back to chats' })).toHaveAttribute('href', '/job-1');
    pathname = '/settings/plugins';
    view.rerender(
      <AgentsLayout>
        <p>plugins</p>
      </AgentsLayout>,
    );
    expect(screen.getByRole('link', { name: 'Back to chats' })).toHaveAttribute('href', '/job-1');
    userId = 'bob';
    view.rerender(
      <AgentsLayout>
        <p>plugins</p>
      </AgentsLayout>,
    );
    expect(screen.getByRole('link', { name: 'Back to chats' })).toHaveAttribute('href', '/');
  });
});

describe('AgentsLayout permissions', () => {
  afterEach(cleanup);

  it('admits a free account with agent.use while hiding administration', () => {
    role = 'free';
    access = { allowed: true, permissions: ['agent.use'] };
    render(
      <AgentsLayout>
        <p>private workspace</p>
      </AgentsLayout>,
    );
    expect(screen.getByText('private workspace')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
  });

  it.each([undefined, { allowed: false, permissions: [] }] as const)(
    'does not admit an admin from role alone with decision %j',
    (decision) => {
      role = 'admin';
      access = decision ? { allowed: decision.allowed, permissions: [] } : undefined;
      render(
        <AgentsLayout>
          <p>private workspace</p>
        </AgentsLayout>,
      );
      expect(screen.queryByText('private workspace')).not.toBeInTheDocument();
      expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    },
  );
});

describe('AgentsLayout owner approval navigation', () => {
  afterEach(cleanup);
  it('keeps the global pending badge in the header when the sidebar is collapsed', async () => {
    localStorage.clear();
    pathname = '/approvals';
    userId = 'ada';
    vi.mocked(listMcpInvocations).mockResolvedValue({
      invocations: Array.from({ length: 50 }, (_, id) => ({ id, status: 'pending' })),
      has_more: true,
      next_before_id: 1,
    } as McpInvocationsApi);
    render(
      <AgentsLayout>
        <p>approvals</p>
      </AgentsLayout>,
    );
    const link = await screen.findByRole('link', { name: 'Approvals (50+ pending)' });
    expect(link).toHaveAttribute('href', '/approvals');
    expect(link).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Agents' })).not.toHaveAttribute('aria-current');
    fireEvent.click(toggle());
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(screen.getByRole('banner')).toContainElement(link);
    expect(listMcpInvocations).toHaveBeenCalledWith({ pendingOnly: true, limit: 50 });
  });

  it('resets counts when the owner changes and removes approvals when admission is revoked', async () => {
    localStorage.clear();
    pathname = '/';
    userId = 'ada';
    vi.mocked(listMcpInvocations).mockResolvedValueOnce({
      invocations: [{ id: 1, status: 'pending' }],
    } as McpInvocationsApi);
    const { rerender } = render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );
    await screen.findByRole('link', { name: 'Approvals (1 pending)' });
    let finish!: (response: McpInvocationsApi) => void;
    vi.mocked(listMcpInvocations).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    userId = 'bob';
    rerender(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );
    expect(screen.queryByRole('link', { name: 'Approvals (1 pending)' })).not.toBeInTheDocument();
    await act(async () => finish({ invocations: [] }));
    expect(screen.getByRole('link', { name: 'Approvals' })).toBeInTheDocument();
    access = { allowed: true, permissions: [] };
    vi.mocked(listMcpInvocations).mockClear();
    rerender(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );
    expect(screen.queryByRole('link', { name: 'Approvals' })).not.toBeInTheDocument();
    expect(listMcpInvocations).not.toHaveBeenCalled();
  });
});

describe('AgentsLayout sign-out', () => {
  afterEach(cleanup);

  // Without admission there is no task list, and with it no account row; the
  // header avatar is then the only way to sign out or switch accounts.
  it('offers the account menu to an account without agent access', () => {
    access = { allowed: false, permissions: [] };
    render(
      <AgentsLayout>
        <p>job</p>
      </AgentsLayout>,
    );
    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(screen.getByRole('banner')).toContainElement(screen.getByTestId('account-menu-header'));
  });
});
