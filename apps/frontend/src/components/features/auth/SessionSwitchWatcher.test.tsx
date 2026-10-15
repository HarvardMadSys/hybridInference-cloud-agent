// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// The watcher as the Agents layout mounts it: around `ProtectedRoute`, under
// the real `AuthProvider`. The session answers after a delay, as a network
// does, so the loading state that unmounts the page is drawn before the answer
// says the account changed.

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';

const router = vi.hoisted(() => ({ replace: vi.fn() }));
let pathname = '/task-1';

vi.mock('next/navigation', () => ({
  useRouter: () => router,
  usePathname: () => pathname,
}));
vi.mock('react-hot-toast', () => ({ default: vi.fn() }));
vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  const { useAuth } = await import('@/components/providers/AuthProvider');
  return {
    useAuth,
    useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key),
  };
});

import { getSessionUser } from '@/lib/api/session';
import { AuthProvider } from '@/components/providers/AuthProvider';
import { ProtectedRoute } from './ProtectedRoute';
import { SessionSwitchWatcher } from './SessionSwitchWatcher';

const access: AgentAccessApi = { allowed: true, permissions: ['agent.use'] };
const SWITCHED =
  'Agents switched to another account in a different tab, so this tab went back to the start.';

function sessionAnswers(id: string) {
  vi.mocked(getSessionUser).mockImplementation(
    () => new Promise((resolve) => setTimeout(() => resolve({ id, agent_access: access }), 20)),
  );
}

function app() {
  return (
    <AuthProvider>
      <SessionSwitchWatcher>
        <ProtectedRoute>
          <p>page</p>
        </ProtectedRoute>
      </SessionSwitchWatcher>
    </AuthProvider>
  );
}

function switchInAnotherTab(id: string) {
  sessionAnswers(id);
  const tab = new BroadcastChannel('cloud-agent:session');
  tab.postMessage({ userId: id });
  tab.close();
}

beforeEach(() => {
  vi.clearAllMocks();
  pathname = '/task-1';
});
afterEach(cleanup);

describe('SessionSwitchWatcher', () => {
  it('takes the tab to the start, without drawing the old page, and says why', async () => {
    sessionAnswers('first');
    const { rerender } = render(app());
    await screen.findByText('page');

    switchInAnotherTab('second');

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith('/'));
    expect(screen.queryByText('page')).toBeNull();
    expect(toast).not.toHaveBeenCalled();

    pathname = '/';
    rerender(app());

    expect(await screen.findByText('page')).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith(SWITCHED);
    expect(router.replace).toHaveBeenCalledTimes(1);
  });

  it('only says why when the tab is at the start already', async () => {
    pathname = '/';
    sessionAnswers('first');
    render(app());
    await screen.findByText('page');

    switchInAnotherTab('second');

    await waitFor(() => expect(toast).toHaveBeenCalledWith(SWITCHED));
    expect(screen.getByText('page')).toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it('leaves a tab alone when it first finds its session', async () => {
    sessionAnswers('first');
    render(app());

    await screen.findByText('page');
    await act(() => new Promise((resolve) => setTimeout(resolve, 30)));

    expect(router.replace).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });
});
