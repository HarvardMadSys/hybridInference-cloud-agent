// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// This page exercises pgAdmin; setup reminders use a configured deployment.
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ id: 'admin-a', access: undefined as AgentAccessApi | undefined }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, agent_access: auth.access } } }),
}));
vi.mock('@/lib/api/adminDatabase', () => ({ getDatabaseTools: vi.fn() }));

import { getDatabaseTools } from '@/lib/api/adminDatabase';
import AdminDatabasePage from './page';

const load = vi.mocked(getDatabaseTools);
const url = 'https://agents.example.org/agents/api/admin/agent/pgadmin/';

beforeEach(() => {
  vi.resetAllMocks();
  auth.id = 'admin-a';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  load.mockResolvedValue({ enabled: true, available: true, url });
});
afterEach(cleanup);

describe('Admin Database', () => {
  it('links to pgAdmin and selects Database in the existing admin navigation', async () => {
    render(<AdminDatabasePage />);
    const link = await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(link).toHaveAttribute('href', url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('link', { name: 'Database' })).toHaveAttribute('aria-current', 'page');
  });

  it('does not contact or expose pgAdmin to a non-admin', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminDatabasePage />);
    expect(screen.getByText('Admin access required.')).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('explains disabled deployments without showing a dead link', async () => {
    load.mockResolvedValue({ enabled: false, available: false, url: null });
    render(<AdminDatabasePage />);
    await screen.findByText(/pgAdmin has not been enabled/);
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('rechecks an unavailable service and enables the working launch link', async () => {
    load.mockResolvedValueOnce({ enabled: true, available: false, url: null });
    render(<AdminDatabasePage />);
    await screen.findByText(/pgAdmin is temporarily unavailable/);
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('removes access when the server rejects the current permission', async () => {
    load.mockRejectedValue(new APIError('FORBIDDEN', 'Permission revoked', 403));
    render(<AdminDatabasePage />);
    await screen.findByText('Admin access required.');
    expect(screen.queryByRole('link', { name: /Open pgAdmin/ })).not.toBeInTheDocument();
  });

  it('aborts the old request and ignores its result when the signed-in user changes', async () => {
    let resolveOld: (value: {
      enabled: boolean;
      available: boolean;
      url: string;
    }) => void = () => {};
    load.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = render(<AdminDatabasePage />);
    const oldSignal = load.mock.calls[0][0];
    auth.id = 'admin-b';
    view.rerender(<AdminDatabasePage />);
    await screen.findByRole('link', { name: /Open pgAdmin/ });
    expect(oldSignal?.aborted).toBe(true);
    resolveOld({ enabled: true, available: true, url: 'https://old.example.org/' });
    await waitFor(() =>
      expect(screen.getByRole('link', { name: /Open pgAdmin/ })).toHaveAttribute('href', url),
    );
  });
});
