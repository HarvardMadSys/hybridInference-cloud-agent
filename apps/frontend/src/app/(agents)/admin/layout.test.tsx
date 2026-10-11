// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from '@/components/providers/AuthProvider';
import { getSessionUser, type AgentAccessApi } from '@/lib/api/session';
import AdminLayout from './layout';

vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));

const readAdminData = vi.fn();
const adminAccess: AgentAccessApi = { allowed: true, permissions: ['agent.use', 'agent.admin'] };

function AdminPage() {
  useEffect(() => {
    readAdminData();
  }, []);
  return <p>Private admin data</p>;
}

function RefreshAccess() {
  const { refreshUser } = useAuth();
  return <button onClick={() => void refreshUser()}>Refresh access</button>;
}

function renderAdmin() {
  return render(
    <AuthProvider>
      <RefreshAccess />
      <AdminLayout>
        <AdminPage />
      </AdminLayout>
    </AuthProvider>,
  );
}

describe('admin layout', () => {
  beforeEach(() => vi.resetAllMocks());
  afterEach(cleanup);

  it('withholds the page while access is loading', () => {
    vi.mocked(getSessionUser).mockReturnValue(new Promise(() => {}));
    renderAdmin();
    expect(screen.queryByText('Private admin data')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: 'Admin access required' }),
    ).not.toBeInTheDocument();
    expect(readAdminData).not.toHaveBeenCalled();
  });

  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: true, permissions: ['agent.use'] },
    { allowed: false, permissions: ['agent.use', 'agent.admin'] },
    { allowed: true, permissions: ['agent.admin'] },
  ])('shows the denial without mounting an unauthorized page for %j', async (agent_access) => {
    vi.mocked(getSessionUser).mockResolvedValue({ id: 'user-1', agent_access });
    renderAdmin();
    expect(await screen.findByRole('heading', { name: 'Admin access required' })).toBeVisible();
    expect(screen.queryByText('Private admin data')).not.toBeInTheDocument();
    expect(readAdminData).not.toHaveBeenCalled();
  });

  it('mounts the page for an explicit admin permission', async () => {
    vi.mocked(getSessionUser).mockResolvedValue({ id: 'user-1', agent_access: adminAccess });
    renderAdmin();
    expect(await screen.findByText('Private admin data')).toBeVisible();
    expect(readAdminData).toHaveBeenCalledOnce();
  });

  it('removes private content while revalidating and then shows a revoked permission', async () => {
    vi.mocked(getSessionUser).mockResolvedValueOnce({ id: 'user-1', agent_access: adminAccess });
    renderAdmin();
    await screen.findByText('Private admin data');
    let resolveAccess!: (value: Awaited<ReturnType<typeof getSessionUser>>) => void;
    vi.mocked(getSessionUser).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveAccess = resolve;
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh access' }));
    await waitFor(() => expect(screen.queryByText('Private admin data')).not.toBeInTheDocument());
    resolveAccess({ id: 'user-1', agent_access: { allowed: true, permissions: ['agent.use'] } });
    expect(await screen.findByRole('heading', { name: 'Admin access required' })).toBeVisible();
    expect(readAdminData).toHaveBeenCalledOnce();
  });
});
