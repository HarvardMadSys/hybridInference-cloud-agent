// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';

vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));

import { sideChatKeys } from '@/components/features/agents/sideChatStorage';
import { endSession, getSessionUser } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';
import { AuthProvider, hasAgentPermission, useAuth } from './AuthProvider';

const getMe = vi.mocked(getSessionUser);

function Consumer() {
  const { state, refreshUser, logout } = useAuth();
  return (
    <>
      <button onClick={() => void logout()}>Sign out</button>
      <output data-testid="auth">{JSON.stringify(state)}</output>
      <button onClick={() => void refreshUser()}>Refresh</button>
    </>
  );
}

function current() {
  return JSON.parse(screen.getByTestId('auth').textContent || '{}');
}

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe('Agent permission validation', () => {
  it.each([
    undefined,
    null,
    {},
    { allowed: true },
    { allowed: 'true', permissions: ['agent.use'] },
    { allowed: false, permissions: ['agent.use', 'agent.admin'] },
    { allowed: true, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use', 'agent.use'] },
    { allowed: true, permissions: ['agent.use', 'future.permission'] },
    { allowed: true, permissions: 'agent.use' },
  ])('denies malformed or missing decisions: %j', (decision) => {
    expect(hasAgentPermission(decision as AgentAccessApi, 'agent.use')).toBe(false);
    expect(hasAgentPermission(decision as AgentAccessApi, 'agent.admin')).toBe(false);
  });

  it('keeps use and administration separate', () => {
    const useOnly: AgentAccessApi = { allowed: true, permissions: ['agent.use'] };
    expect(hasAgentPermission(useOnly, 'agent.use')).toBe(true);
    expect(hasAgentPermission(useOnly, 'agent.admin')).toBe(false);
  });
});

describe('AuthProvider current decision', () => {
  it('accepts explicit administration for an identified account', async () => {
    getMe.mockResolvedValue({
      id: 'u1',

      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().loading).toBe(false));
    expect(current().isAuthenticated).toBe(true);
    expect(hasAgentPermission(current().user.agent_access, 'agent.admin')).toBe(true);
  });

  it('keeps an identity response identifiable without granting access', async () => {
    getMe.mockResolvedValue({ id: 'u1' });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().loading).toBe(false));
    expect(current().isAuthenticated).toBe(true);
    expect(hasAgentPermission(current().user.agent_access, 'agent.admin')).toBe(false);
    expect(hasAgentPermission(current().user.agent_access, 'agent.use')).toBe(false);
  });

  it('replaces granted permissions with a revoked decision on refresh', async () => {
    getMe.mockResolvedValue({
      id: 'u1',

      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() =>
      expect(hasAgentPermission(current().user?.agent_access, 'agent.admin')).toBe(true),
    );
    getMe.mockResolvedValue({
      id: 'u1',

      agent_access: { allowed: false, permissions: [] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(hasAgentPermission(current().user?.agent_access, 'agent.admin')).toBe(false),
    );
    expect(current().isAuthenticated).toBe(true);
    expect(current().user.agent_access.allowed).toBe(false);
  });

  it('does not retain previous permissions when refresh is unavailable', async () => {
    getMe.mockResolvedValue({
      id: 'u1',

      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() =>
      expect(hasAgentPermission(current().user?.agent_access, 'agent.admin')).toBe(true),
    );
    getMe.mockRejectedValue(new Error('Gateway unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(current().isAuthenticated).toBe(false));
    expect(current().user).toBeNull();
    expect(current().error).toBe('unavailable');
  });
});

it('does not retain profile fields from an older API response', async () => {
  getMe.mockResolvedValue(
    Object.assign(
      { id: 'u1', agent_access: { allowed: true, permissions: ['agent.use' as const] } },
      { email: 'sentinel@example.test', user_name: 'Sentinel', role: 'admin' },
    ),
  );
  render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  );
  await waitFor(() => expect(current().loading).toBe(false));
  expect(current().user).toEqual({
    id: 'u1',
    agent_access: { allowed: true, permissions: ['agent.use'] },
  });
});

it('clears only the signing-out user’s unsent side drafts', async () => {
  getMe.mockResolvedValue({ id: 'draft-owner' });
  const own = sideChatKeys('draft-owner', 'main').draft;
  const other = sideChatKeys('other-owner', 'main').draft;
  sessionStorage.setItem(own, 'private draft');
  sessionStorage.setItem(other, 'other draft');
  render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  );
  await waitFor(() => expect(current().loading).toBe(false));
  fireEvent.click(screen.getByText('Sign out'));
  await waitFor(() => expect(current().isAuthenticated).toBe(false));
  expect(sessionStorage.getItem(own)).toBeNull();
  expect(sessionStorage.getItem(other)).toBe('other draft');
});

// The session is a signed token, and only the server's answer deletes the
// cookie carrying it. Showing a signed-out app after a failed request left
// the session working for whoever next opened this browser.
it('stays signed in, drafts kept, when the server did not end the session', async () => {
  getMe.mockResolvedValue({ id: 'draft-owner' });
  vi.mocked(endSession).mockRejectedValueOnce(
    new APIError('SERVICE_UNAVAILABLE', 'HTTP 502: Bad Gateway', 502),
  );
  const own = sideChatKeys('draft-owner', 'main').draft;
  sessionStorage.setItem(own, 'private draft');
  let logout: (() => Promise<void>) | undefined;
  function Capture() {
    const auth = useAuth();
    logout = auth.logout;
    return <output data-testid="auth">{JSON.stringify(auth.state)}</output>;
  }
  render(
    <AuthProvider>
      <Capture />
    </AuthProvider>,
  );
  await waitFor(() => expect(current().loading).toBe(false));

  await act(async () => {
    await expect(logout!()).rejects.toMatchObject({ statusCode: 502 });
  });

  expect(current().isAuthenticated).toBe(true);
  expect(current().user.id).toBe('draft-owner');
  expect(sessionStorage.getItem(own)).toBe('private draft');
});

describe('AuthProvider and the browser’s other Agents tabs', () => {
  const access: AgentAccessApi = { allowed: true, permissions: ['agent.use'] };

  // Another tab of the same browser, as BroadcastChannel sees one.
  function announceFromAnotherTab(userId: string | null) {
    const tab = new BroadcastChannel('cloud-agent:session');
    tab.postMessage({ userId });
    tab.close();
  }

  async function renderAs(id: string) {
    getMe.mockResolvedValue({ id, agent_access: access });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().user?.id).toBe(id));
  }

  it('follows another tab that switched the session, and keeps that it did', async () => {
    let auth: ReturnType<typeof useAuth> | undefined;
    function Switched() {
      auth = useAuth();
      return null;
    }
    getMe.mockResolvedValue({ id: 'first', agent_access: access });
    render(
      <AuthProvider>
        <Consumer />
        <Switched />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().user?.id).toBe('first'));
    expect(auth?.sessionSwitched).toBe(false);
    getMe.mockResolvedValue({ id: 'second', agent_access: access });

    announceFromAnotherTab('second');

    await waitFor(() => expect(current().user?.id).toBe('second'));
    expect(auth?.sessionSwitched).toBe(true);
    act(() => auth?.acknowledgeSessionSwitch());
    expect(auth?.sessionSwitched).toBe(false);
  });

  it('signs this tab out when another one signed the session out', async () => {
    await renderAs('first');
    getMe.mockResolvedValue(null);

    announceFromAnotherTab(null);

    await waitFor(() => expect(current().isAuthenticated).toBe(false));
  });

  it('ignores an announcement of the account it already shows', async () => {
    await renderAs('first');
    getMe.mockClear();

    announceFromAnotherTab('first');
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(getMe).not.toHaveBeenCalled();
  });

  it('checks again when the tab is looked at', async () => {
    await renderAs('first');
    getMe.mockResolvedValue({ id: 'second', agent_access: access });
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 60_000);

    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => expect(current().user?.id).toBe('second'));
    vi.restoreAllMocks();
  });

  it('shows no loading state when that check finds the same account', async () => {
    await renderAs('first');
    getMe.mockClear();
    const loading: boolean[] = [];
    const observer = new MutationObserver(() => loading.push(current().loading));
    observer.observe(screen.getByTestId('auth'), {
      childList: true,
      characterData: true,
      subtree: true,
    });
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 60_000);

    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => expect(getMe).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    observer.disconnect();
    vi.restoreAllMocks();
    expect(loading).not.toContain(true);
    expect(current().user?.id).toBe('first');
  });

  function slowRead() {
    let answer!: (me: { id: string; agent_access: AgentAccessApi } | null) => void;
    getMe.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    return (me: { id: string; agent_access: AgentAccessApi } | null) => act(async () => answer(me));
  }

  it('lets a sign-out in another tab outlast a first read asked before it', async () => {
    const reply = slowRead();
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    getMe.mockResolvedValue(null);

    announceFromAnotherTab(null);
    await waitFor(() => expect(getMe).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(current().loading).toBe(false));
    await reply({ id: 'first', agent_access: access });

    expect(current().isAuthenticated).toBe(false);
    expect(current().user).toBeNull();
  });

  it('keeps the account another tab switched to when an older read answers last', async () => {
    let auth: ReturnType<typeof useAuth> | undefined;
    function Switched() {
      auth = useAuth();
      return null;
    }
    getMe.mockResolvedValue({ id: 'first', agent_access: access });
    render(
      <AuthProvider>
        <Consumer />
        <Switched />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().user?.id).toBe('first'));
    const reply = slowRead();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    getMe.mockResolvedValue({ id: 'second', agent_access: access });

    announceFromAnotherTab('second');
    await waitFor(() => expect(current().user?.id).toBe('second'));
    act(() => auth?.acknowledgeSessionSwitch());
    await reply({ id: 'first', agent_access: access });

    expect(current().user?.id).toBe('second');
    expect(auth?.sessionSwitched).toBe(false);
  });

  it('stays signed out when a read asked before signing out answers after it', async () => {
    await renderAs('first');
    vi.mocked(endSession).mockResolvedValue(undefined);
    const reply = slowRead();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(current().isAuthenticated).toBe(false));
    await reply({ id: 'first', agent_access: access });

    expect(current().isAuthenticated).toBe(false);
    expect(current().user).toBeNull();
  });

  it('tells the other tabs when it signs out', async () => {
    await renderAs('first');
    vi.mocked(endSession).mockResolvedValue(undefined);
    const heard: unknown[] = [];
    const tab = new BroadcastChannel('cloud-agent:session');
    tab.onmessage = (event) => heard.push(event.data);

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => expect(heard).toContainEqual({ userId: null }));
    tab.close();
  });
});
