// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminConversationsPage } from '@/lib/api/adminConversations';
import { getAdminConversations } from '@/lib/api/adminConversations';
import { getAdminRuns } from '@/lib/api/adminRuns';
import type { AdminSandbox } from '@/lib/api/adminSandboxes';
import { getAdminSandbox, getAdminSandboxes } from '@/lib/api/adminSandboxes';
import { APIError } from '@/lib/utils/errors';

const context = vi.hoisted(() => ({ query: '' }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'admin',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    },
  }),
}));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(context.query),
}));
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
vi.mock('@/lib/api/adminConversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminConversations')>()),
  getAdminConversations: vi.fn(),
}));
vi.mock('@/lib/api/adminRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminRuns')>()),
  getAdminRuns: vi.fn(),
}));
vi.mock('@/lib/api/adminSandboxes', () => ({
  getAdminSandboxes: vi.fn(),
  getAdminSandbox: vi.fn(),
  stopAdminSandbox: vi.fn(),
}));

import ActivityPage from './page';

const since = '2026-10-01T08:00:00Z';
const until = '2026-10-02T08:00:00Z';
const observedAt = '2026-10-10T10:00:00Z';
const scope = {
  since,
  until,
  user_id: 'linked-owner',
  repo: 'org/linked-repo',
  runtime: 'claude-code',
  model: 'linked-model',
  host: 'linked-host',
  state: 'failed',
};

function conversationPage(): AdminConversationsPage {
  return {
    conversations: [
      {
        thread_id: 'linked-session',
        title: 'Linked session',
        user_id: scope.user_id,
        repo: scope.repo,
        created_at: since,
        last_activity_at: until,
        archived_at: null,
        deleted_at: null,
        state: 'idle',
        turn_count: 1,
        latest_job_id: 'linked-job',
        latest_turn_no: 1,
        latest_turn_state: 'failed',
        latest_runtime: scope.runtime,
        latest_model: scope.model,
        last_matching_turn_at: since,
      },
    ],
    since,
    until,
    next_cursor: null,
  };
}

function sandbox(): AdminSandbox {
  return {
    id: 'sandbox-one',
    state: 'running',
    origin_thread_id: 'sandbox-session',
    title: 'Sandbox session',
    repo: 'org/sandbox-repo',
    user_id: 'sandbox-owner',
    host: 'sandbox-host',
    container_id: 'container-one-id',
    container_name: 'container-one',
    runtime_id: 'codex',
    runtime_version: '1.2.3',
    execution_profile: 'trusted-v1',
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 1,
    active_attempt_count: 0,
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: '2026-10-10T10:15:00Z',
    updated_at: '2026-10-10T09:59:50Z',
    host_last_heartbeat_at: observedAt,
  };
}

function sandboxPage() {
  return {
    sandboxes: [sandbox()],
    next_cursor: null,
    total_count: 1,
    observed_at: observedAt,
  };
}

function sandboxDetail() {
  return { sandbox: sandbox(), members: [], observed_at: observedAt };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  context.query = '';
  vi.mocked(getAdminConversations).mockResolvedValue(conversationPage());
  vi.mocked(getAdminRuns).mockResolvedValue({ runs: [], next_cursor: null, since, until });
  vi.mocked(getAdminSandboxes).mockResolvedValue(sandboxPage());
  vi.mocked(getAdminSandbox).mockResolvedValue(sandboxDetail());
});
afterEach(cleanup);

describe('sandboxes in admin activity', () => {
  it('keeps the linked turn selection while displaying sandbox-specific controls', async () => {
    context.query = new URLSearchParams(scope).toString();
    render(<ActivityPage />);
    await screen.findByRole('link', { name: 'Linked session' });
    expect(screen.getByText(/User: linked-owner/)).toBeInTheDocument();
    expect(screen.getByLabelText('Turn result')).toHaveValue('failed');

    fireEvent.click(screen.getByRole('button', { name: 'Sandboxes' }));
    await screen.findByText('sandbox-one');
    expect(screen.getByRole('button', { name: 'Sandboxes', pressed: true })).toBeInTheDocument();
    expect(screen.queryByLabelText('Turn created')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Turn result')).not.toBeInTheDocument();
    expect(screen.queryByText(/User: linked-owner/)).not.toBeInTheDocument();
    expect(screen.queryByText('Oct 1 – Oct 2')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sandbox state')).toHaveValue('running');
    expect(screen.getAllByRole('button', { name: 'Refresh' })).toHaveLength(1);
    expect(getAdminSandboxes).toHaveBeenCalledWith({ limit: 200 }, expect.any(AbortSignal));
    expect(getAdminConversations).toHaveBeenCalledTimes(1);
    expect(getAdminRuns).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Sandbox state'), { target: { value: 'all' } });
    await waitFor(() =>
      expect(getAdminSandboxes).toHaveBeenLastCalledWith(
        { limit: 200, state: 'all' },
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Turns' }));
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(1));
    expect(getAdminRuns).toHaveBeenCalledWith({ ...scope, limit: 50 }, expect.any(AbortSignal));
    expect(screen.getByLabelText('Turn result')).toHaveValue('failed');
    expect(screen.getByLabelText('Turn created')).toHaveValue('custom');
    expect(screen.getByText(/User: linked-owner/)).toBeInTheDocument();
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    await screen.findByRole('link', { name: 'Linked session' });
    expect(getAdminConversations).toHaveBeenLastCalledWith(
      { ...scope, limit: 50 },
      expect.any(AbortSignal),
    );
  });

  it('opens the sandbox level directly without requesting sessions or turns', async () => {
    context.query = 'level=sandboxes';
    render(<ActivityPage />);
    await screen.findByText('sandbox-one');
    expect(screen.getByRole('button', { name: 'Sandboxes', pressed: true })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Activity' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getAllByRole('heading', { name: 'Admin' })).toHaveLength(1);
    expect(screen.queryByLabelText('Turn created')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Turn result')).not.toBeInTheDocument();
    expect(getAdminConversations).not.toHaveBeenCalled();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('aborts a pending sandbox inventory when returning to sessions', async () => {
    const pending = deferred<ReturnType<typeof sandboxPage>>();
    vi.mocked(getAdminSandboxes).mockReturnValueOnce(pending.promise);
    render(<ActivityPage />);
    await screen.findByRole('link', { name: 'Linked session' });
    fireEvent.click(screen.getByRole('button', { name: 'Sandboxes' }));
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(1));
    const signal = vi.mocked(getAdminSandboxes).mock.calls[0][1]!;
    fireEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    await screen.findByRole('link', { name: 'Linked session' });
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(sandboxPage()));
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
  });

  it.each([401, 403])(
    'revokes Activity access and cancels sandbox details after HTTP %s',
    async (status) => {
      const pending = deferred<ReturnType<typeof sandboxDetail>>();
      vi.mocked(getAdminSandbox).mockReturnValueOnce(pending.promise);
      render(<ActivityPage />);
      await screen.findByRole('link', { name: 'Linked session' });
      fireEvent.click(screen.getByRole('button', { name: 'Sandboxes' }));
      fireEvent.click(
        await screen.findByRole('button', { name: 'Show details for Sandbox session' }),
      );
      await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(1));
      const signal = vi.mocked(getAdminSandbox).mock.calls[0][1]!;
      vi.mocked(getAdminSandboxes).mockRejectedValueOnce(
        new APIError('FORBIDDEN', 'Denied', status),
      );
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await screen.findByText('Admin access required');
      expect(signal.aborted).toBe(true);
      expect(screen.queryByRole('group', { name: 'Level' })).not.toBeInTheDocument();
      expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'Linked session' })).not.toBeInTheDocument();
      await act(async () => pending.resolve(sandboxDetail()));
      expect(screen.getByText('Admin access required')).toBeInTheDocument();
      expect(screen.queryByText('Sandbox session')).not.toBeInTheDocument();
      expect(getAdminConversations).toHaveBeenCalledTimes(1);
    },
  );
});
