// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';

vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));

import { sideChatKeys } from '@/components/features/agents/sideChatStorage';
import { getSessionUser } from '@/lib/api/session';
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
