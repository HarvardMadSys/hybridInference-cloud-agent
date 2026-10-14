// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

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

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteAdminSandbox,
  getAdminSandboxTrajectory,
  type AdminSandbox,
  type AdminSandboxDetail,
  type AdminSandboxTrajectory,
  type AdminSandboxTrajectoryEvent,
} from '@/lib/api/adminSandboxes';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({
  id: 'admin-one',
  access: undefined as AgentAccessApi | undefined,
}));
const navigation = vi.hoisted(() => ({
  sandboxId: 'sandbox/one',
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, agent_access: auth.access } } }),
}));
vi.mock('next/navigation', () => ({
  useParams: () => ({ sandboxId: navigation.sandboxId }),
}));
vi.mock('@/lib/api/adminSandboxes', () => ({
  deleteAdminSandbox: vi.fn(),
  getAdminSandboxTrajectory: vi.fn(),
}));

import AdminSandboxTrajectoryPage from './page';

const observedAt = '2026-10-10T10:00:00Z';

function sandbox(overrides: Partial<AdminSandbox> = {}): AdminSandbox {
  return {
    id: 'sandbox/one',
    origin_thread_id: 'thread/one',
    title: 'Fix login',
    repo: 'org/repo',
    user_id: 'owner-one',
    host: 'host-one',
    container_id: 'container-one',
    container_name: 'agent-sandbox-one',
    runtime_id: 'codex',
    runtime_version: '1.2.3',
    execution_profile: 'trusted-v1',
    state: 'stopped',
    generation: 4,
    stop_requested_at: observedAt,
    archived_at: null,
    deletion_state: 'none',
    deletion_requested_at: null,
    deletion_request_id: null,
    deletion_archive_key: null,
    deletion_sha256: null,
    deletion_size_bytes: null,
    deleted_at: null,
    deletion_error: null,
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 1,
    active_attempt_count: 0,
    active_operation_count: 0,
    snapshot_state: 'none',
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: null,
    updated_at: observedAt,
    host_last_heartbeat_at: observedAt,
    ...overrides,
  };
}

function event(
  id: number,
  eventType: string,
  createdAt: string,
  overrides: Partial<AdminSandboxTrajectoryEvent> = {},
): AdminSandboxTrajectoryEvent {
  return {
    id,
    generation: 4,
    event_type: eventType,
    from_state: null,
    to_state: null,
    source: 'system',
    actor_id: null,
    created_at: createdAt,
    details: {},
    ...overrides,
  };
}

function trajectory(overrides: Partial<AdminSandboxTrajectory> = {}): AdminSandboxTrajectory {
  return {
    sandbox: sandbox(),
    members: [
      {
        thread_id: 'thread/one',
        title: 'Main session',
        user_id: 'owner-one',
        resident_slot_id: 0,
        resident_state: 'stopped',
        context_status: 'ready',
        native_state: 'ready',
        active_attempt_id: null,
        last_settled_job_id: 'job-one',
        created_at: '2026-10-10T09:00:00Z',
        updated_at: observedAt,
      },
    ],
    member_history_count: 1,
    member_history_truncated: false,
    events: [
      event(2, 'sandbox.stopped', '2026-10-10T09:02:00Z', {
        from_state: 'running',
        to_state: 'stopped',
        source: 'host',
      }),
      event(1, 'sandbox.created', '2026-10-10T09:00:00Z', {
        to_state: 'provisioning',
        details: { image_digest: 'sha256:abc' },
      }),
    ],
    observed_at: observedAt,
    next_cursor: null,
    ...overrides,
  };
}

function pendingDetail(): AdminSandboxDetail {
  return {
    sandbox: sandbox({
      deletion_state: 'pending',
      deletion_requested_at: '2026-10-10T10:01:00Z',
    }),
    members: trajectory().members,
    observed_at: '2026-10-10T10:01:00Z',
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.id = 'admin-one';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  navigation.sandboxId = 'sandbox/one';
  vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(trajectory());
  vi.mocked(deleteAdminSandbox).mockResolvedValue(pendingDetail());
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('admin sandbox trajectory', () => {
  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.use', 'agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not read cross-user history without current admin access: %j', (access) => {
    auth.access = access;
    render(<AdminSandboxTrajectoryPage />);
    expect(screen.getByRole('heading', { name: 'Admin access required' })).toBeInTheDocument();
    expect(getAdminSandboxTrajectory).not.toHaveBeenCalled();
  });

  it('renders lifecycle events chronologically with states, sources, timestamps, and details', async () => {
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByRole('heading', { name: 'Fix login' })).toBeInTheDocument();
    expect(getAdminSandboxTrajectory).toHaveBeenCalledWith(
      'sandbox/one',
      { limit: 200 },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'Main session' })).toHaveAttribute(
      'href',
      '/admin/sessions/thread%2Fone',
    );

    const list = screen.getByRole('list', { name: 'Sandbox trajectory' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Sandbox created');
    expect(rows[0]).toHaveTextContent('unknown → provisioning');
    expect(rows[0]).toHaveTextContent('Source: system');
    expect(rows[0]).toHaveTextContent('Image digest');
    expect(rows[0]).toHaveTextContent('sha256:abc');
    expect(rows[1]).toHaveTextContent('Sandbox stopped');
    expect(rows[1]).toHaveTextContent('running → stopped');
    expect(rows[1]).toHaveTextContent('Source: host');
    expect(rows[0].querySelector('time')).toHaveTextContent('Oct 10, 2026, 9:00:00 AM UTC');
  });

  it('shows the latest restorable checkpoint, or none', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        latest_checkpoint: {
          id: 'c'.repeat(32),
          generation: 1,
          host: 'runner-one',
          size_bytes: 12 * 1024 * 1024,
          new_layer_bytes: 12 * 1024 * 1024,
          state: 'reported',
          captured_at: '2026-10-10T09:05:00Z',
          recorded_at: '2026-10-10T09:05:10Z',
        },
      }),
    );
    render(<AdminSandboxTrajectoryPage />);

    const label = await screen.findByText('Latest checkpoint');
    expect(label.nextElementSibling).toHaveTextContent('12 MiB');
    expect(label.nextElementSibling).toHaveTextContent('Oct 10, 2026');
    cleanup();

    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(trajectory());
    render(<AdminSandboxTrajectoryPage />);
    expect((await screen.findByText('Latest checkpoint')).nextElementSibling).toHaveTextContent(
      'None',
    );
  });

  it('shows when the bounded member history omits older rows', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        member_history_count: 300,
        member_history_truncated: true,
      }),
    );

    render(<AdminSandboxTrajectoryPage />);

    expect(
      await screen.findByRole('heading', { name: 'Member sessions (300)' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Showing 1 of 300 historical member sessions',
    );
  });

  it('renders a deleted tombstone and its final lifecycle event on direct access', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        sandbox: sandbox({
          state: 'lost',
          container_id: null,
          deletion_state: 'deleted',
          deletion_requested_at: '2026-10-10T10:01:00Z',
          deletion_archive_key: 's3://sandbox-archives/sandbox-one.tar.gz',
          deletion_sha256: 'a'.repeat(64),
          deletion_size_bytes: 12_345,
          deleted_at: '2026-10-10T10:02:00Z',
        }),
        events: [
          event(3, 'deleted', '2026-10-10T10:02:00Z', {
            from_state: 'stopped',
            to_state: 'deleted',
            source: 'host',
          }),
        ],
      }),
    );
    render(<AdminSandboxTrajectoryPage />);

    expect(
      await screen.findByText(/Sandbox deleted Oct 10, 2026, 10:02:00 AM UTC/),
    ).toBeInTheDocument();
    expect(screen.getByText('s3://sandbox-archives/sandbox-one.tar.gz')).toHaveProperty(
      'tagName',
      'CODE',
    );
    expect(screen.getByText(/Archive size: 12,345 bytes/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
    const timeline = screen.getByRole('list', { name: 'Sandbox trajectory' });
    expect(within(timeline).getByText('Deleted')).toBeInTheDocument();
  });
  it('loads and merges another trajectory page without duplicating events', async () => {
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(trajectory({ next_cursor: 'next+page' }))
      .mockResolvedValueOnce(
        trajectory({
          events: [
            event(1, 'sandbox.created', '2026-10-10T09:00:00Z'),
            event(3, 'sandbox.started', '2026-10-10T09:01:00Z', {
              from_state: 'starting',
              to_state: 'running',
            }),
          ],
          next_cursor: null,
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Load more events' }));
    await waitFor(() => expect(getAdminSandboxTrajectory).toHaveBeenCalledTimes(2));
    expect(getAdminSandboxTrajectory).toHaveBeenLastCalledWith(
      'sandbox/one',
      { limit: 200, cursor: 'next+page' },
      expect.any(AbortSignal),
    );

    const rows = within(screen.getByRole('list', { name: 'Sandbox trajectory' })).getAllByRole(
      'listitem',
    );
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('Sandbox created'),
      expect.stringContaining('Sandbox started'),
      expect.stringContaining('Sandbox stopped'),
    ]);
    expect(screen.queryByRole('button', { name: 'Load more events' })).not.toBeInTheDocument();
  });

  it('does not let stale pagination overwrite a newer deletion tombstone', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let resolveOlder!: (value: AdminSandboxTrajectory) => void;
    const olderPage = new Promise<AdminSandboxTrajectory>((resolve) => {
      resolveOlder = resolve;
    });
    const pending = trajectory({
      sandbox: sandbox({
        deletion_state: 'pending',
        deletion_requested_at: '2026-10-10T10:01:00Z',
      }),
      next_cursor: 'older-events',
    });
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(pending)
      .mockReturnValueOnce(olderPage)
      .mockResolvedValueOnce(
        trajectory({
          sandbox: sandbox({
            state: 'lost',
            container_id: null,
            deletion_state: 'deleted',
            deletion_requested_at: '2026-10-10T10:01:00Z',
            deletion_archive_key: 's3://sandbox-archives/sandbox-one.tar.gz',
            deleted_at: '2026-10-10T10:02:00Z',
          }),
          events: [
            event(3, 'deleted', '2026-10-10T10:02:00Z', {
              from_state: 'stopped',
              to_state: 'deleted',
              source: 'host',
            }),
          ],
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Load more events' }));
    await waitFor(() => expect(getAdminSandboxTrajectory).toHaveBeenCalledTimes(2));
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(
      await screen.findByText(/Sandbox deleted Oct 10, 2026, 10:02:00 AM UTC/),
    ).toBeInTheDocument();

    await act(async () => {
      resolveOlder(
        trajectory({
          sandbox: pending.sandbox,
          events: [event(1, 'sandbox.created', '2026-10-10T09:00:00Z')],
          next_cursor: null,
          observed_at: '2026-10-10T10:01:30Z',
        }),
      );
      await olderPage;
    });

    expect(screen.getByText(/Sandbox deleted Oct 10, 2026, 10:02:00 AM UTC/)).toBeInTheDocument();
    expect(screen.queryByText(/Archiving to S3/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
  });

  it('deletes only a stopped sandbox and retains the completed trajectory', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(trajectory())
      .mockResolvedValueOnce(
        trajectory({
          sandbox: sandbox({
            state: 'lost',
            container_id: null,
            deletion_state: 'deleted',
            deletion_requested_at: '2026-10-10T10:01:00Z',
            deletion_archive_key: 's3://sandbox-archives/sandbox-one.tar.gz',
            deletion_sha256: 'b'.repeat(64),
            deletion_size_bytes: 4_096,
            deleted_at: '2026-10-10T10:02:00Z',
          }),
          events: [
            event(3, 'deleted', '2026-10-10T10:02:00Z', {
              from_state: 'stopped',
              to_state: 'deleted',
              source: 'host',
            }),
          ],
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('uploaded to S3'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('verified'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/native resume/i));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('cannot be undone'));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox/one',
      {
        generation: 4,
        container_id: 'container-one',
        last_activity_at: '2026-10-10T09:59:00Z',
      },
      expect.any(AbortSignal),
      false,
      false,
    );
    expect(
      await screen.findByText(/The retained container will be deleted only/),
    ).toHaveTextContent(/deleted only after the upload is verified/i);
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(
      await screen.findByText(/Sandbox deleted Oct 10, 2026, 10:02:00 AM UTC/),
    ).toHaveTextContent(/archived and verified in S3/i);
    expect(screen.getByText(/Chat history and lifecycle history remain/)).toBeInTheDocument();
    expect(screen.getByText('s3://sandbox-archives/sandbox-one.tar.gz')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
    const timeline = screen.getByRole('list', { name: 'Sandbox trajectory' });
    expect(within(timeline).getByText('Deleted')).toBeInTheDocument();
  });

  it('keeps the sandbox unchanged when archive-before-delete confirmation is cancelled', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
    expect(screen.queryByText(/Archiving to S3/)).not.toBeInTheDocument();
  });

  it('shows a verified archive receipt while container deletion waits to retry', async () => {
    const archiveKey = 's3://sandbox-archives/sandbox-one.tar.gz';
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        sandbox: sandbox({
          deletion_state: 'archived',
          deletion_requested_at: '2026-10-10T10:01:00Z',
          deletion_archive_key: archiveKey,
          deletion_sha256: 'c'.repeat(64),
          deletion_size_bytes: 98_765,
          deletion_error: 'The host could not remove the retained container.',
        }),
      }),
    );
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByText(/Archive verified in S3/)).toHaveTextContent(
      /Waiting for the host to delete the retained container/,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Deletion is waiting to retry: The host could not remove the retained container.',
    );
    expect(screen.getByText(archiveKey)).toHaveProperty('tagName', 'CODE');
    expect(screen.getByText(/Archive size: 98,765 bytes/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Deleting archived sandbox…' })).toBeDisabled();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('deletes the stopped sandbox without an archive after confirmation', async () => {
    vi.mocked(deleteAdminSandbox).mockResolvedValue({
      sandbox: sandbox({
        deletion_state: 'pending',
        deletion_discard: true,
        deletion_requested_at: '2026-10-10T10:01:00Z',
      }),
      members: [],
      observed_at: '2026-10-10T10:01:00Z',
    });
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete without archive' }));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringMatching(/without uploading it to S3/),
    );
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/cannot be restored/));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox/one',
      {
        generation: 4,
        container_id: 'container-one',
        last_activity_at: '2026-10-10T09:59:00Z',
      },
      expect.any(AbortSignal),
      false,
      true,
    );
    expect(await screen.findByText(/without an S3 archive/)).toBeInTheDocument();
    expect(screen.getByText(/No S3 archive is being uploaded/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Deleting without archive…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deletion in progress' })).toBeDisabled();
  });

  it('allows a safely failed archive request to be confirmed and retried', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        sandbox: sandbox({
          deletion_state: 'failed',
          deletion_requested_at: '2026-10-10T10:01:00Z',
          deletion_error: 'S3 upload failed.',
        }),
      }),
    );
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Deletion failed: S3 upload failed.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry archive and delete' }));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox/one',
      {
        generation: 4,
        container_id: 'container-one',
        last_activity_at: '2026-10-10T09:59:00Z',
      },
      expect.any(AbortSignal),
      false,
      false,
    );
    expect(
      await screen.findByText(/The retained container will be deleted only/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Deletion failed:/)).not.toBeInTheDocument();
  });

  it.each([false, true])(
    'verifies a stale running record before deletion (discard=%s)',
    async (discard) => {
      vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
        trajectory({ sandbox: sandbox({ state: 'running' }) }),
      );
      render(<AdminSandboxTrajectoryPage />);

      fireEvent.click(
        await screen.findByRole('button', {
          name: discard ? 'Delete without archive' : 'Verify stopped and force delete',
        }),
      );
      expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('stopped or dead'));
      expect(window.confirm).toHaveBeenCalledWith(
        expect.stringContaining('does not stop a running container'),
      );
      expect(deleteAdminSandbox).toHaveBeenCalledWith(
        'sandbox/one',
        {
          generation: 4,
          container_id: 'container-one',
          last_activity_at: '2026-10-10T09:59:00Z',
        },
        expect.any(AbortSignal),
        true,
        discard,
      );
    },
  );

  it('does not force-delete a running sandbox that still has active work', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({ sandbox: sandbox({ state: 'running', active_attempt_count: 1 }) }),
    );
    render(<AdminSandboxTrajectoryPage />);
    expect(
      await screen.findByRole('button', { name: 'Verify stopped and force delete' }),
    ).toBeDisabled();
    expect(screen.getByText('Wait for all active turns to finish.')).toBeInTheDocument();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it.each([
    [{ active_attempt_count: 1 }, 'Wait for all active turns to finish.'],
    [{ active_operation_count: 1 }, 'Wait for active workspace operations to finish.'],
    [{ snapshot_state: 'snapshotting' }, 'Wait for the workspace snapshot to finish.'],
    [{ generation: undefined }, 'The retained container identity is unavailable.'],
    [{ container_id: null }, 'The retained container identity is unavailable.'],
  ] as const)('blocks deletion for unsafe stopped state %j', async (overrides, reason) => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({ sandbox: sandbox(overrides) }),
    );
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('clears trajectory data when the server rejects current admin access', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockRejectedValue(
      new APIError('FORBIDDEN', 'Admin access required', 403),
    );
    render(<AdminSandboxTrajectoryPage />);
    expect(
      await screen.findByRole('heading', { name: 'Admin access required' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('owner-one')).not.toBeInTheDocument();
  });
});
