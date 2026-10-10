// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import Link from 'next/link';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAccessApi } from '@/lib/api/session';
import { SkillDialog } from '@/components/features/agents/skills/SkillDialog';

let access: AgentAccessApi | undefined;
let role = 'internal';

beforeEach(() => {
  access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  role = 'internal';
});

let pathname = '/';
let statusUrl = '/status';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => pathname,
}));

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({
      state: { user: { user_name: 'Ada', email: 'ada@example.com', role, agent_access: access } },
      logout: vi.fn(),
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

vi.mock('@/components/features/agents/AgentsSidebar', () => ({
  AgentsSidebar: ({ collapsed }: { collapsed?: boolean }) =>
    collapsed ? null : (
      <aside id="agents-sidebar" data-testid="sidebar-stub">
        tasks
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

  it('collapses and restores the task list from the header toggle', () => {
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

    fireEvent.click(toggle());

    expect(screen.queryByTestId('sidebar-stub')).not.toBeInTheDocument();
    expect(toggle()).toHaveAccessibleName('Expand task list');
    expect(toggle()).toHaveAttribute('aria-expanded', 'false');
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('1');

    fireEvent.click(toggle());

    expect(screen.getByTestId('sidebar-stub')).toBeInTheDocument();
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

  it.each(['/archived', '/integrations'])(
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
