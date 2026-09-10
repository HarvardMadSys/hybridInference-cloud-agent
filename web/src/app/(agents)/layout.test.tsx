// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
      state: { user: { user_name: 'Ada', email: 'ada@example.com', role: 'internal' } },
      logout: vi.fn(),
    }),
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});

vi.mock('@/components/providers/AuthProvider', () => ({
  hasRole: () => true,
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { appName: 'FreeInference', orgName: '', orgUrl: '', statusUrl },
    features: { rag: false },
  }),
}));

vi.mock('@/components/features/agents/AgentsSidebar', () => ({
  AgentsSidebar: ({ collapsed }: { collapsed?: boolean }) =>
    collapsed ? null : <aside data-testid="sidebar-stub">tasks</aside>,
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

  it('moves the highlight to Admin on the admin page', () => {
    pathname = '/admin/hosts';

    renderLayout();

    expect(screen.getByRole('link', { name: 'Admin' })).toHaveAttribute('aria-current', 'page');

    // And Agents is a way back rather than a label: it was a bare <span>, so
    // the only route off this page was the browser's back button.
    const agents = screen.getByRole('link', { name: 'Agents' });
    expect(agents).not.toHaveAttribute('aria-current');
    expect(agents).toHaveAttribute('href', '/');
  });
});
