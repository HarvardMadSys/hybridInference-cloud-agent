// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  archiveAdminSandbox,
  deleteAdminSandbox,
  getAdminSandbox,
  restoreAdminSandbox,
  stopAdminSandbox,
  type AdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';
import { APIError } from '@/lib/utils/errors';

vi.mock('@/lib/api/adminSandboxes', () => ({
  archiveAdminSandbox: vi.fn(),
  deleteAdminSandbox: vi.fn(),
  getAdminSandbox: vi.fn(),
  restoreAdminSandbox: vi.fn(),
  stopAdminSandbox: vi.fn(),
}));

import { SandboxManagement } from './SandboxManagement';

const observedAt = '2026-10-10T10:00:00Z';
function detail(overrides: Partial<AdminSandbox> = {}): AdminSandboxDetail {
  return {
    observed_at: observedAt,
    sandbox: {
      id: 'sandbox-one',
      origin_thread_id: 'thread-one',
      title: 'Fix login',
      user_id: 'owner-one',
      repo: 'org/repo',
      host: 'host-one',
      state: 'running',
      container_id: 'container-one',
      container_name: 'sandbox-one',
      generation: 4,
      runtime_id: 'codex',
      runtime_version: '1',
      execution_profile: 'trusted-v1',
      memory_limit: '4g',
      cpu_limit: '2',
      member_count: 2,
      active_attempt_count: 0,
      active_operation_count: 0,
      terminal_count: 0,
      terminal_count_observed_at: observedAt,
      created_at: observedAt,
      last_activity_at: observedAt,
      idle_deadline: null,
      updated_at: observedAt,
      host_last_heartbeat_at: observedAt,
      ...overrides,
    },
    members: [
      {
        thread_id: 'thread/one',
        title: 'Main session',
        user_id: 'owner-one',
        resident_slot_id: 1,
        resident_state: 'idle',
        context_status: 'ready',
        native_state: 'running',
        active_attempt_id: null,
        last_settled_job_id: 'job-one',
        created_at: observedAt,
        updated_at: observedAt,
      },
    ],
  };
}

const onDenied = vi.fn();
const onChanged = vi.fn();
function mount(autoRefresh = true) {
  return render(
    <SandboxManagement
      sandboxId="sandbox-one"
      nowMs={performance.now()}
      autoRefresh={autoRefresh}
      onDenied={onDenied}
      onChanged={onChanged}
    />,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getAdminSandbox).mockResolvedValue(detail());
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('sandbox member visibility and control', () => {
  it('reloads after a Strict Mode effect cleanup aborts the first read', async () => {
    render(
      <StrictMode>
        <SandboxManagement
          sandboxId="sandbox-one"
          nowMs={performance.now()}
          onDenied={onDenied}
          onChanged={onChanged}
        />
      </StrictMode>,
    );
    expect(await screen.findByRole('link', { name: 'Main session' })).toBeInTheDocument();
    expect(getAdminSandbox).toHaveBeenCalledTimes(2);
    expect(vi.mocked(getAdminSandbox).mock.calls[0][1]?.aborted).toBe(true);
  });

  it('shows member session links and current residency metadata', async () => {
    mount();
    expect(await screen.findByRole('link', { name: 'Main session' })).toHaveAttribute(
      'href',
      '/admin/sessions/thread%2Fone',
    );
    expect(screen.getByRole('list', { name: 'Sandbox sessions' })).toHaveTextContent(
      'Resident: idle · Context: ready · Agent: running',
    );
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeEnabled();
  });

  it.each([
    [{ active_attempt_count: 1 }, 'Wait for all active turns'],
    [{ active_operation_count: 1 }, 'Wait for active workspace operations'],
    [{ snapshot_state: 'pending' }, 'Wait for the workspace snapshot'],
    [{ snapshot_state: 'snapshotting' }, 'Wait for the workspace snapshot'],
    [{ snapshot_state: 'importing' }, 'Wait for the workspace snapshot'],
    [{ terminal_count: 1 }, 'Close all open terminals'],
    [{ terminal_count: null }, 'Terminal activity is unavailable. Refresh details'],
    [
      { terminal_count_observed_at: '2026-10-10T09:57:00Z' },
      'Terminal activity is stale. Refresh details',
    ],
    [{ generation: undefined }, 'container identity is unavailable'],
  ] as const)(
    'does not offer stopping when the reviewed snapshot is unsafe: %j',
    async (overrides, message) => {
      vi.mocked(getAdminSandbox).mockResolvedValue(detail(overrides));
      mount();
      expect(await screen.findByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
      expect(screen.getByText(new RegExp(message))).toBeInTheDocument();
      expect(stopAdminSandbox).not.toHaveBeenCalled();
    },
  );

  it.each([
    { terminal_count: null, terminal_count_observed_at: null },
    { terminal_count: 0, terminal_count_observed_at: observedAt },
    { terminal_count: 2, terminal_count_observed_at: observedAt },
    { terminal_count: 0, terminal_count_observed_at: '2026-10-10T09:57:00Z' },
    { terminal_count: 2, terminal_count_observed_at: '2026-10-10T09:57:00Z' },
  ])(
    'shows the broker recovery message until refreshed activity is current: %j',
    async (overrides) => {
      const message =
        "The host's workspace broker is unavailable. Restore its connection and refresh details.";
      vi.mocked(getAdminSandbox)
        .mockResolvedValueOnce({ ...detail(overrides), terminal_activity_error: message })
        .mockResolvedValue(detail({ terminal_count: 0, terminal_count_observed_at: observedAt }));
      mount();
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
      expect(stopAdminSandbox).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Refresh details' }));
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeEnabled(),
      );
      expect(getAdminSandbox).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(message)).not.toBeInTheDocument();
      expect(stopAdminSandbox).not.toHaveBeenCalled();
    },
  );

  it.each([
    [{ active_attempt_count: 1 }, 'Wait for all active turns to finish.'],
    [{ active_operation_count: 1 }, 'Wait for active workspace operations to finish.'],
    [{ snapshot_state: 'pending' }, 'Wait for the workspace snapshot to finish.'],
  ] as const)(
    'keeps the current activity blocker visible ahead of broker errors: %j',
    async (overrides, message) => {
      const brokerError = "The host's workspace broker is unavailable.";
      vi.mocked(getAdminSandbox).mockResolvedValue({
        ...detail(overrides),
        terminal_activity_error: brokerError,
      });
      mount();
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(screen.queryByText(brokerError)).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
      expect(stopAdminSandbox).not.toHaveBeenCalled();
    },
  );

  it('confirms the shared effect and posts exactly the reviewed container and activity', async () => {
    vi.mocked(stopAdminSandbox).mockResolvedValue(
      detail({ state: 'stopping', stop_requested_at: observedAt }),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop idle sandbox' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('shared by 2 sessions'));
    expect(stopAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      {
        generation: 4,
        container_id: 'container-one',
        last_activity_at: observedAt,
      },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText(/Stop requested\. Waiting for the host/)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
  });

  it('does not post when the operator cancels the confirmation', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop idle sandbox' }));
    expect(stopAdminSandbox).not.toHaveBeenCalled();
  });

  it('keeps a refusal visible and refreshes newer activity before another stop', async () => {
    vi.mocked(stopAdminSandbox).mockRejectedValue(new APIError('CONFLICT', 'Sandbox is busy', 409));
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail())
      .mockResolvedValue(detail({ active_attempt_count: 1 }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop idle sandbox' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Sandbox is busy');
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('polls until the host reports the requested stop as complete', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail({ state: 'stopping', stop_requested_at: observedAt }))
      .mockResolvedValue(detail({ state: 'stopped' }));
    mount();
    expect(await screen.findByText(/Stop requested\. Waiting for the host/)).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText('The host reports this sandbox as stopped.')).toBeInTheDocument();
    expect(screen.queryByText(/Stop requested\. Waiting for the host/)).not.toBeInTheDocument();
  });

  it('pauses detail polling while preserving manual refresh and resumes when enabled', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const view = mount(false);
    await screen.findByRole('link', { name: 'Main session' });
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandbox).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh details' }));
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(2));
    view.rerender(
      <SandboxManagement
        sandboxId="sandbox-one"
        nowMs={performance.now()}
        autoRefresh
        onDenied={onDenied}
        onChanged={onChanged}
      />,
    );
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(getAdminSandbox).toHaveBeenCalledTimes(3);
  });

  it('keeps an outstanding detail read alive when auto refresh is paused', async () => {
    let resolve!: (value: AdminSandboxDetail) => void;
    vi.mocked(getAdminSandbox).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = mount();
    const signal = vi.mocked(getAdminSandbox).mock.calls[0][1];
    view.rerender(
      <SandboxManagement
        sandboxId="sandbox-one"
        nowMs={performance.now()}
        autoRefresh={false}
        onDenied={onDenied}
        onChanged={onChanged}
      />,
    );
    expect(signal?.aborted).toBe(false);
    expect(getAdminSandbox).toHaveBeenCalledOnce();
    await act(async () => resolve(detail()));
    expect(screen.getByRole('link', { name: 'Main session' })).toBeInTheDocument();
  });

  it('does not attribute an automatic stop to an administrator', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopping' }));
    mount();
    expect(await screen.findByText(/Container is stopping\./)).toBeInTheDocument();
    expect(screen.queryByText(/Stop requested\./)).not.toBeInTheDocument();
  });

  it('clears access after a stop returns an authorization error', async () => {
    vi.mocked(stopAdminSandbox).mockRejectedValue(new APIError('FORBIDDEN', 'no', 403));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop idle sandbox' }));
    await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
    expect(screen.queryByRole('link', { name: 'Main session' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh details' })).toBeDisabled();
  });

  it('removes only a stopped sandbox from inventory with a reviewed activity fence', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', container_id: null, terminal_count: null }),
    );
    vi.mocked(archiveAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', container_id: null, archived_at: observedAt }),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('Workspace files, native sessions, and chat history are retained.'),
    );
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('shared by 2 sessions'));
    expect(archiveAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      { generation: 4, container_id: null, last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole('button', { name: 'Restore to inventory' })).toBeEnabled();
    expect(screen.getByText(/Removed from the admin inventory\./)).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(stopAdminSandbox).not.toHaveBeenCalled();
  });

  it.each([
    { state: 'running' },
    { state: 'stopping' },
    { state: 'stopped_dirty' },
    { state: 'lost' },
    { state: 'stopped', active_attempt_count: 1 },
    { state: 'stopped', active_operation_count: 1 },
    { state: 'stopped', snapshot_state: 'pending' },
    { state: 'stopped', snapshot_state: 'snapshotting' },
    { state: 'stopped', snapshot_state: 'importing' },
    { state: 'stopped', generation: undefined },
  ] as const)('refuses inventory removal for an unsafe reviewed state: %j', async (overrides) => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail(overrides));
    mount();
    expect(await screen.findByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(archiveAdminSandbox).not.toHaveBeenCalled();
  });

  it('keeps inventory unchanged when removal confirmation is cancelled', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
    vi.mocked(window.confirm).mockReturnValue(false);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
    expect(archiveAdminSandbox).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('restores a removed sandbox using its current identity without changing lifecycle state', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ archived_at: observedAt }));
    vi.mocked(restoreAdminSandbox).mockResolvedValue(detail({ archived_at: null }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Restore to inventory' }));
    expect(restoreAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      { generation: 4, container_id: 'container-one', last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(screen.queryByText(/Removed from the admin inventory\./)).not.toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(window.confirm).not.toHaveBeenCalled();
    expect(stopAdminSandbox).not.toHaveBeenCalled();
  });

  it('disables controls when the reviewed detail snapshot becomes stale', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
    const view = mount();
    await screen.findByRole('link', { name: 'Main session' });
    view.rerender(
      <SandboxManagement
        sandboxId="sandbox-one"
        nowMs={performance.now() + 90_001}
        onDenied={onDenied}
        onChanged={onChanged}
      />,
    );
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(screen.getByText('Refresh details before changing this sandbox.')).toBeInTheDocument();
  });

  it('refreshes instead of removing when confirmation outlives the reviewed snapshot', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
    mount();
    await screen.findByRole('link', { name: 'Main session' });
    vi.mocked(window.confirm).mockImplementation(() => {
      vi.spyOn(performance, 'now').mockReturnValue(performance.now() + 90_001);
      return true;
    });
    fireEvent.click(screen.getByRole('button', { name: 'Remove from inventory' }));
    expect(archiveAdminSandbox).not.toHaveBeenCalled();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Refresh details before changing this sandbox.',
    );
    expect(getAdminSandbox).toHaveBeenCalledTimes(2);
  });

  it('refreshes a refused removal before allowing another action', async () => {
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail({ state: 'stopped' }))
      .mockResolvedValue(detail({ state: 'running', active_attempt_count: 1 }));
    vi.mocked(archiveAdminSandbox).mockRejectedValue(
      new APIError('CONFLICT', 'Sandbox changed after review', 409),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Sandbox changed after review');
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it.each([401, 403])(
    'clears member data after removal loses access with HTTP %i',
    async (status) => {
      vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
      vi.mocked(archiveAdminSandbox).mockRejectedValue(new APIError('FORBIDDEN', 'no', status));
      mount();
      fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
      await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
      expect(screen.queryByRole('link', { name: 'Main session' })).not.toBeInTheDocument();
      expect(onChanged).not.toHaveBeenCalled();
      expect(getAdminSandbox).toHaveBeenCalledOnce();
    },
  );

  it('blocks overlapping actions and aborts removal on unmount without applying a late result', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
    let resolve!: (value: AdminSandboxDetail) => void;
    vi.mocked(archiveAdminSandbox).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
    expect(screen.getByRole('button', { name: 'Removing…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh details' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
    const signal = vi.mocked(archiveAdminSandbox).mock.calls[0][2];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(detail({ state: 'stopped', archived_at: observedAt })));
    expect(onChanged).not.toHaveBeenCalled();
    expect(onDenied).not.toHaveBeenCalled();
  });

  it('aborts an outstanding member read on unmount and ignores a late result', async () => {
    let resolve!: (value: AdminSandboxDetail) => void;
    vi.mocked(getAdminSandbox).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = mount();
    const signal = vi.mocked(getAdminSandbox).mock.calls[0][1];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(detail()));
    expect(onChanged).not.toHaveBeenCalled();
    expect(onDenied).not.toHaveBeenCalled();
  });
});

describe('archive before physical sandbox deletion', () => {
  beforeEach(() => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ state: 'stopped' }));
  });

  it('confirms the shared loss of live resume and sends the reviewed container fence', async () => {
    vi.mocked(deleteAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', deletion_state: 'pending', deletion_requested_at: observedAt }),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('shared by 2 sessions'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/S3/));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/native resume/i));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      { generation: 4, container_id: 'container-one', last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText(/Archiving to S3/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(archiveAdminSandbox).not.toHaveBeenCalled();
    expect(stopAdminSandbox).not.toHaveBeenCalled();
  });

  it('does not request deletion when its confirmation is cancelled', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it.each([
    { state: 'running' },
    { state: 'stopping' },
    { state: 'stopped_dirty' },
    { state: 'lost' },
    { state: 'stopped', active_attempt_count: 1 },
    { state: 'stopped', active_operation_count: 1 },
    { state: 'stopped', snapshot_state: 'pending' },
    { state: 'stopped', snapshot_state: 'snapshotting' },
    { state: 'stopped', snapshot_state: 'importing' },
    { state: 'stopped', generation: undefined },
    { state: 'stopped', container_id: null },
    { state: 'stopped', deletion_state: 'pending' },
    { state: 'stopped', deletion_state: 'archived' },
    { state: 'stopped', deletion_state: 'deleted' },
  ] as const)('blocks physical deletion for an unsafe reviewed sandbox: %j', async (overrides) => {
    vi.mocked(getAdminSandbox).mockResolvedValue(detail(overrides));
    mount();
    expect(await screen.findByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('uses a clean host stop even when no terminal report remains', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', terminal_count: null, terminal_count_observed_at: null }),
    );
    mount();
    expect(await screen.findByRole('button', { name: 'Archive to S3 and delete' })).toBeEnabled();
  });

  it.each(['pending', 'archived'] as const)(
    'blocks inventory restoration during %s deletion while preserving manual monitoring',
    async (deletionState) => {
      vi.mocked(getAdminSandbox).mockResolvedValue(
        detail({ state: 'stopped', archived_at: observedAt, deletion_state: deletionState }),
      );
      mount();
      expect(await screen.findByRole('button', { name: 'Restore to inventory' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Refresh details' })).toBeEnabled();
      expect(restoreAdminSandbox).not.toHaveBeenCalled();
    },
  );

  it('blocks deletion after the reviewed detail snapshot becomes stale', async () => {
    const view = mount();
    await screen.findByRole('link', { name: 'Main session' });
    view.rerender(
      <SandboxManagement
        sandboxId="sandbox-one"
        nowMs={performance.now() + 90_001}
        onDenied={onDenied}
        onChanged={onChanged}
      />,
    );
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('refreshes instead of deleting when confirmation outlives the reviewed snapshot', async () => {
    mount();
    await screen.findByRole('link', { name: 'Main session' });
    vi.mocked(window.confirm).mockImplementation(() => {
      vi.spyOn(performance, 'now').mockReturnValue(performance.now() + 90_001);
      return true;
    });
    fireEvent.click(screen.getByRole('button', { name: 'Archive to S3 and delete' }));
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Refresh details before changing this sandbox.',
    );
    expect(getAdminSandbox).toHaveBeenCalledTimes(2);
  });

  it('polls through verified archival before reporting completed deletion', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail({ state: 'stopped', deletion_state: 'pending' }))
      .mockResolvedValueOnce(
        detail({
          state: 'stopped',
          deletion_state: 'archived',
          deletion_archive_key: 'sandbox-archives/sandbox-one.tar.gz',
        }),
      )
      .mockResolvedValue(
        detail({
          state: 'lost',
          container_id: null,
          deletion_state: 'deleted',
          deletion_archive_key: 'sandbox-archives/sandbox-one.tar.gz',
          deleted_at: observedAt,
          archived_at: observedAt,
        }),
      );
    mount();
    expect(await screen.findByText(/Archiving to S3/)).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText(/Archive verified in S3/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText(/Sandbox deleted\./)).toHaveTextContent(
      /live native resume is no longer available/i,
    );
    expect(screen.queryByText(/Archiving to S3/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Archive verified in S3/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('renders the archived S3 reference as text without treating it as a trusted link', async () => {
    const archiveKey = 'javascript:alert(document.cookie)';
    vi.mocked(getAdminSandbox).mockResolvedValue(
      detail({
        state: 'lost',
        container_id: null,
        deletion_state: 'deleted',
        deletion_archive_key: archiveKey,
        deletion_sha256: 'a'.repeat(64),
        deletion_size_bytes: 12345,
        deleted_at: observedAt,
        archived_at: observedAt,
      }),
    );
    mount();
    const key = await screen.findByText(archiveKey);
    expect(key.tagName).toBe('CODE');
    expect(key.closest('p')).toHaveTextContent(`S3 archive: ${archiveKey}`);
    expect(screen.getByText(/Sandbox deleted\./)).toHaveTextContent(/archived in S3/i);
    for (const link of screen.getAllByRole('link')) {
      expect(link).not.toHaveAttribute('href', archiveKey);
    }
  });

  it('hides a restored deleted record again while preserving its S3 receipt', async () => {
    const deleted = {
      state: 'lost',
      container_id: null,
      deletion_state: 'deleted',
      deletion_archive_key: 'sandbox-archives/sandbox-one.tar.gz',
      deleted_at: observedAt,
    } as const;
    vi.mocked(getAdminSandbox).mockResolvedValue(detail({ ...deleted, archived_at: null }));
    vi.mocked(archiveAdminSandbox).mockResolvedValue(
      detail({ ...deleted, archived_at: observedAt }),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Remove from inventory' }));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining('Its S3 archive and chat history remain available.'),
    );
    const confirmation = vi.mocked(window.confirm).mock.calls[0][0];
    expect(confirmation).not.toMatch(/native sessions.*retained|resuming a session/i);
    expect(archiveAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      { generation: 4, container_id: null, last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(await screen.findByRole('button', { name: 'Restore to inventory' })).toBeEnabled();
    expect(screen.getByText('sandbox-archives/sandbox-one.tar.gz')).toBeInTheDocument();
    expect(screen.getByText(/Sandbox deleted\./)).toHaveTextContent(
      /live native resume is no longer available/i,
    );
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(onChanged).toHaveBeenCalledOnce();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('keeps deletion progress visible with paused polling and allows manual refresh', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail({ state: 'stopped', deletion_state: 'pending' }))
      .mockResolvedValue(detail({ state: 'stopped', deletion_state: 'archived' }));
    mount(false);
    expect(await screen.findByText(/Archiving to S3/)).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandbox).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh details' }));
    expect(await screen.findByText(/Archive verified in S3/)).toBeInTheDocument();
    expect(getAdminSandbox).toHaveBeenCalledTimes(2);
  });

  it('shows an archive failure and retries deletion from the current reviewed identity', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', deletion_state: 'failed', deletion_error: 'S3 upload failed' }),
    );
    vi.mocked(deleteAdminSandbox).mockResolvedValue(
      detail({ state: 'stopped', deletion_state: 'pending', deletion_error: null }),
    );
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Deletion failed: S3 upload failed');
    fireEvent.click(screen.getByRole('button', { name: 'Archive to S3 and delete' }));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox-one',
      { generation: 4, container_id: 'container-one', last_activity_at: observedAt },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText(/Archiving to S3/)).toBeInTheDocument();
    expect(screen.queryByText(/Deletion failed:/)).not.toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it('keeps a verified archive visible while the host retries container deletion', async () => {
    vi.mocked(getAdminSandbox).mockResolvedValue(
      detail({
        state: 'stopped',
        deletion_state: 'archived',
        deletion_archive_key: 'sandbox-archives/sandbox-one.tar.gz',
        deletion_error: 'Container removal failed',
      }),
    );
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Deletion is waiting to retry: Container removal failed',
    );
    expect(screen.getByText(/Archive verified in S3/)).toBeInTheDocument();
    expect(screen.getByText('sandbox-archives/sandbox-one.tar.gz')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('refreshes a refused deletion before allowing another destructive action', async () => {
    vi.mocked(getAdminSandbox)
      .mockResolvedValueOnce(detail({ state: 'stopped' }))
      .mockResolvedValue(detail({ state: 'running', active_attempt_count: 1 }));
    vi.mocked(deleteAdminSandbox).mockRejectedValue(
      new APIError('CONFLICT', 'Sandbox changed after review', 409),
    );
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Sandbox changed after review');
    await waitFor(() => expect(getAdminSandbox).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Archive to S3 and delete' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it.each([401, 403])('clears access when deletion returns HTTP %i', async (status) => {
    vi.mocked(deleteAdminSandbox).mockRejectedValue(new APIError('FORBIDDEN', 'no', status));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
    expect(screen.queryByRole('link', { name: 'Main session' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Archive to S3 and delete' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh details' })).toBeDisabled();
    expect(getAdminSandbox).toHaveBeenCalledOnce();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('blocks overlapping mutations and aborts a deletion request on unmount', async () => {
    let resolve!: (value: AdminSandboxDetail) => void;
    vi.mocked(deleteAdminSandbox).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Archive to S3 and delete' }));
    expect(screen.getByRole('button', { name: 'Requesting deletion…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Refresh details' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove from inventory' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeDisabled();
    const signal = vi.mocked(deleteAdminSandbox).mock.calls[0][2];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(detail({ state: 'stopped', deletion_state: 'pending' })));
    expect(onChanged).not.toHaveBeenCalled();
    expect(onDenied).not.toHaveBeenCalled();
  });
});
