// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAdminSandbox,
  stopAdminSandbox,
  type AdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';
import { APIError } from '@/lib/utils/errors';

vi.mock('@/lib/api/adminSandboxes', () => ({
  getAdminSandbox: vi.fn(),
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
function mount() {
  return render(
    <SandboxManagement
      sandboxId="sandbox-one"
      nowMs={performance.now()}
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
