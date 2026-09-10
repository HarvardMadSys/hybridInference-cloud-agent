// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';

vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));

import { getSessionUser } from '@/lib/api/session';
import { AuthProvider, hasAgentPermission, useAuth } from './AuthProvider';

const getMe = vi.mocked(getSessionUser);

function Consumer() {
  const { state, refreshUser } = useAuth();
  return (
    <>
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
  it('accepts explicit administration for a free account', async () => {
    getMe.mockResolvedValue({
      id: 'u1',
      email: null,
      role: 'free',
      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().loading).toBe(false));
    expect(current().isAuthenticated).toBe(true);
    expect(current().user.is_admin).toBe(true);
  });

  it('keeps an old admin profile identifiable without granting access', async () => {
    getMe.mockResolvedValue({ id: 'u1', email: null, role: 'admin' });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().loading).toBe(false));
    expect(current().isAuthenticated).toBe(true);
    expect(current().user.is_admin).toBe(false);
    expect(hasAgentPermission(current().user.agent_access, 'agent.use')).toBe(false);
  });

  it('replaces granted permissions with a revoked decision on refresh', async () => {
    getMe.mockResolvedValue({
      id: 'u1',
      email: null,
      role: 'admin',
      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().user?.is_admin).toBe(true));
    getMe.mockResolvedValue({
      id: 'u1',
      email: null,
      role: 'admin',
      agent_access: { allowed: false, permissions: [] },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(current().user?.is_admin).toBe(false));
    expect(current().isAuthenticated).toBe(true);
    expect(current().user.agent_access.allowed).toBe(false);
  });

  it('does not retain previous permissions when refresh is unavailable', async () => {
    getMe.mockResolvedValue({
      id: 'u1',
      email: null,
      role: 'admin',
      agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
    });
    render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>,
    );
    await waitFor(() => expect(current().user?.is_admin).toBe(true));
    getMe.mockRejectedValue(new Error('Gateway unavailable'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(current().isAuthenticated).toBe(false));
    expect(current().user).toBeNull();
  });
});
