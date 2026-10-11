// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// Setup notices have their own tests; this page uses a configured deployment.
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

import type { AdminSandbox, AdminSandboxResourceUsage } from '@/lib/api/adminSandboxes';
import { getAdminSandbox, getAdminSandboxes, stopAdminSandbox } from '@/lib/api/adminSandboxes';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({
  id: 'admin-one',
  access: undefined as AgentAccessApi | undefined,
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, agent_access: auth.access } } }),
}));
vi.mock('@/lib/api/adminSandboxes', () => ({
  getAdminSandboxes: vi.fn(),
  getAdminSandbox: vi.fn(),
  stopAdminSandbox: vi.fn(),
}));

import AdminSandboxesPage from './page';

type InventoryPage = Awaited<ReturnType<typeof getAdminSandboxes>>;
const observedAt = '2026-10-10T10:00:00Z';
function sandbox(overrides: Partial<AdminSandbox> = {}): AdminSandbox {
  return {
    id: 'sandbox-one',
    state: 'running',
    origin_thread_id: 'thread/one',
    title: 'Fix login',
    repo: 'org/repo-one',
    user_id: 'owner-one',
    host: 'host-one',
    container_id: 'container-one-id',
    container_name: 'container-one',
    runtime_id: 'codex',
    runtime_version: '1.2.3',
    execution_profile: 'trusted-v1',
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 3,
    active_attempt_count: 1,
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: '2026-10-10T10:15:00Z',
    updated_at: '2026-10-10T09:59:50Z',
    host_last_heartbeat_at: observedAt,
    ...overrides,
  };
}
function usage(overrides: Partial<AdminSandboxResourceUsage> = {}): AdminSandboxResourceUsage {
  return {
    source: 'docker-stats',
    scope: 'container',
    sample_id: 'sample-one',
    sampled_at: '2026-10-10T09:59:55Z',
    received_at: observedAt,
    age_seconds: 5,
    stale: false,
    cpu_cores: 0.25,
    cpu_window_seconds: 30,
    memory_bytes: 1024 ** 2,
    memory_limit_bytes: 4 * 1024 ** 3,
    pids: 2,
    io_read_bytes: 0,
    io_write_bytes: 1024,
    net_rx_bytes: 2048,
    net_tx_bytes: 4096,
    io_read_bytes_per_second: 0,
    io_write_bytes_per_second: 1,
    net_rx_bytes_per_second: 2,
    net_tx_bytes_per_second: 4,
    ...overrides,
  };
}

function statValue(label: string, scope: HTMLElement = document.body): HTMLElement {
  const term = within(scope).getByText(label, { selector: 'dt' });
  const value = term.nextElementSibling;
  expect(value).not.toBeNull();
  return value as HTMLElement;
}

function page(rows = [sandbox()], next: string | null = null, total = rows.length): InventoryPage {
  return { sandboxes: rows, next_cursor: next, total_count: total, observed_at: observedAt };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function inventoryTitles(): (string | null)[] {
  return within(screen.getByRole('table'))
    .getAllByRole('link')
    .filter((link) => link.getAttribute('href')?.startsWith('/admin/sessions/'))
    .map((link) => link.textContent);
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.id = 'admin-one';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  vi.mocked(getAdminSandboxes).mockResolvedValue(page());
  vi.mocked(getAdminSandbox).mockResolvedValue({
    sandbox: sandbox(),
    members: [],
    observed_at: observedAt,
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('running sandbox inventory', () => {
  it('loads all lifecycle states without carrying over the previous filter snapshot', async () => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    const next = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes).mockReturnValueOnce(next.promise);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox state' }), {
      target: { value: 'all' },
    });
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(getAdminSandboxes).toHaveBeenLastCalledWith(
        { limit: 200, state: 'all' },
        expect.any(AbortSignal),
      ),
    );
    await act(async () => next.resolve(page([sandbox({ state: 'stopped', id: 'stopped-one' })])));
    expect(await screen.findByText('stopped-one')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'All sandboxes (1)' })).toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveTextContent('stopped');
    expect(statValue('Running sandboxes')).toHaveTextContent('0');
  });

  it('clears private inventory if a sandbox detail read loses admin access', async () => {
    vi.mocked(getAdminSandbox).mockRejectedValue(new APIError('FORBIDDEN', 'no', 403));
    render(<AdminSandboxesPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByText('owner-one')).not.toBeInTheDocument();
  });

  it('switches to all states after a stop request so host confirmation remains visible', async () => {
    const idle = sandbox({
      generation: 4,
      active_attempt_count: 0,
      active_operation_count: 0,
      terminal_count: 0,
      terminal_count_observed_at: observedAt,
    });
    vi.mocked(getAdminSandbox).mockResolvedValue({
      sandbox: idle,
      members: [],
      observed_at: observedAt,
    });
    vi.mocked(stopAdminSandbox).mockResolvedValue({
      sandbox: { ...idle, state: 'stopping', stop_requested_at: observedAt },
      members: [],
      observed_at: observedAt,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AdminSandboxesPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Stop idle sandbox' }));
    expect(await screen.findByText(/Stop requested\. Waiting for the host/)).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Sandbox state' })).toHaveValue('all');
    expect(getAdminSandboxes).toHaveBeenLastCalledWith(
      { limit: 200, state: 'all' },
      expect.any(AbortSignal),
    );
  });

  it('aborts a fleet read begun before a stop and ignores its late running state', async () => {
    const idle = sandbox({
      generation: 4,
      active_attempt_count: 0,
      active_operation_count: 0,
      terminal_count: 0,
      terminal_count_observed_at: observedAt,
    });
    const stopping = { ...idle, state: 'stopping' as const };
    const oldRead = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(page([idle]))
      .mockReturnValueOnce(oldRead.promise)
      .mockResolvedValue(page([stopping]));
    vi.mocked(getAdminSandbox).mockResolvedValue({
      sandbox: idle,
      members: [],
      observed_at: observedAt,
    });
    vi.mocked(stopAdminSandbox).mockResolvedValue({
      sandbox: stopping,
      members: [],
      observed_at: observedAt,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<AdminSandboxesPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Stop idle sandbox' })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(2));
    const oldSignal = vi.mocked(getAdminSandboxes).mock.calls[1][1];
    fireEvent.click(screen.getByRole('button', { name: 'Stop idle sandbox' }));
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(3));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => oldRead.resolve(page([idle])));
    const row = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    expect(row).toHaveTextContent('stopping');
    expect(row).not.toHaveTextContent('Retained');
  });

  it('stops fleet polling after a detail read loses admin permission', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandbox).mockRejectedValue(new APIError('FORBIDDEN', 'no', 403));
    render(<AdminSandboxesPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
    await screen.findByText('Admin access required');
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  });
  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not read inventory without a current admin permission: %j', (access) => {
    auth.access = access;
    render(<AdminSandboxesPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getAdminSandboxes).not.toHaveBeenCalled();
  });

  it('automatically loads every page, including idle containers and other owners', async () => {
    const rest = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(page([sandbox()], 'next+page', 2))
      .mockReturnValueOnce(rest.promise);
    render(<AdminSandboxesPage />);
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(2));
    const first = vi.mocked(getAdminSandboxes).mock.calls[0];
    const second = vi.mocked(getAdminSandboxes).mock.calls[1];
    expect(first[0]).not.toHaveProperty('user_id');
    expect(second[0]).toMatchObject({ cursor: 'next+page' });
    expect(second[1]).toBe(first[1]);
    expect(first[1]).toBeInstanceOf(AbortSignal);
    expect(screen.getByText('Loading running sandboxes…')).toBeInTheDocument();
    await act(async () => {
      rest.resolve(
        page([
          sandbox({
            id: 'sandbox-two',
            origin_thread_id: 'thread-two',
            title: 'Second session',
            user_id: 'owner-two',
            host: 'host-two',
            active_attempt_count: 0,
            member_count: 1,
          }),
        ]),
      );
    });
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    expect(within(table).getByText('owner-one')).toBeInTheDocument();
    expect(within(table).getByText('owner-two')).toBeInTheDocument();
    expect(within(table).getByText('Executing')).toBeInTheDocument();
    expect(within(table).getByText('Retained')).toBeInTheDocument();
    expect(screen.queryByText('Loading running sandboxes…')).not.toBeInTheDocument();
    expect(
      within(screen.getByRole('navigation', { name: 'Admin sections' })).getByRole('link', {
        name: 'Sandboxes',
      }),
    ).toHaveAttribute('aria-current', 'page');
  });

  it('keeps sandbox groups separate and links the origin session and host', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox(),
        sandbox({ id: 'sandbox-two', origin_thread_id: 'thread-two', member_count: 1 }),
      ]),
    );
    render(<AdminSandboxesPage />);
    const sessions = await screen.findAllByRole('link', { name: 'Fix login' });
    expect(sessions.map((link) => link.getAttribute('href'))).toEqual([
      '/admin/sessions/thread%2Fone',
      '/admin/sessions/thread-two',
    ]);
    const table = screen.getByRole('table');
    expect(within(table).getByText('sandbox-one')).toBeInTheDocument();
    expect(within(table).getByText('sandbox-two')).toBeInTheDocument();
    const firstRow = sessions[0].closest('tr')!;
    expect(within(firstRow).getByRole('link', { name: 'host-one' })).toHaveAttribute(
      'href',
      '/admin/hosts',
    );
    expect(firstRow).toHaveTextContent('container-one');
    expect(firstRow).toHaveTextContent('codex');
    expect(firstRow).toHaveTextContent('1.2.3');
    expect(firstRow).toHaveTextContent('4g');
    expect(firstRow).toHaveTextContent('3');
  });

  it('shows stale and unknown host heartbeats without hiding their sandboxes', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({ host_last_heartbeat_at: '2026-10-10T09:57:00Z' }),
        sandbox({ id: 'sandbox-two', title: null, host_last_heartbeat_at: null }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    expect(screen.getByText('Stale')).toBeInTheDocument();
    expect(screen.getByText('Unknown')).toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveTextContent('sandbox-two');
  });

  it('marks a host heartbeat stale after three missed thirty-second heartbeats', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(observedAt));
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    vi.mocked(getAdminSandboxes).mockRejectedValue(new APIError('SERVER_ERROR', 'boom', 500));
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(90_000));
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(screen.getByText('Stale')).toBeInTheDocument();
  });

  it('uses server observation time for freshness when the browser system clock is skewed', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date('2046-10-10T10:00:00Z'));
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
    const row = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    expect(row).toHaveTextContent('just now');
    vi.setSystemTime(new Date('2056-10-10T10:00:00Z'));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
    expect(row).toHaveTextContent('just now');
  });

  it('includes the time spent loading later pages in the first page’s heartbeat age', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(observedAt));
    const rest = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(page([sandbox()], 'next', 2))
      .mockReturnValueOnce(rest.promise);
    render(<AdminSandboxesPage />);
    await act(async () => {});
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(100_000));
    const laterObservation = '2026-10-10T10:01:40Z';
    await act(async () =>
      rest.resolve({
        ...page([
          sandbox({
            id: 'sandbox-two',
            title: 'Second session',
            origin_thread_id: 'thread-two',
            host_last_heartbeat_at: laterObservation,
          }),
        ]),
        observed_at: laterObservation,
      }),
    );
    const firstRow = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    const secondRow = screen.getByRole('link', { name: 'Second session' }).closest('tr')!;
    expect(within(firstRow).getByText('Stale')).toBeInTheDocument();
    expect(within(secondRow).queryByText('Stale')).not.toBeInTheDocument();
  });

  it('states a successful empty inventory', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(page([]));
    render(<AdminSandboxesPage />);
    expect(await screen.findByText('No running sandboxes.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports an initial read failure without claiming that no sandboxes are running', async () => {
    vi.mocked(getAdminSandboxes).mockRejectedValue(new APIError('SERVER_ERROR', 'boom', 500));
    render(<AdminSandboxesPage />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText('No running sandboxes.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('reports a repeated pagination cursor instead of displaying an incomplete inventory', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(page([sandbox()], 'repeated-cursor', 10));
    render(<AdminSandboxesPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('complete sandbox list');
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    expect(screen.queryByText('No running sandboxes.')).not.toBeInTheDocument();
  });

  it('refreshes manually and replaces the complete inventory when containers disappear', async () => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    vi.mocked(getAdminSandboxes).mockResolvedValueOnce(page([]));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('No running sandboxes.');
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    expect(vi.mocked(getAdminSandboxes).mock.calls[1][0]).not.toHaveProperty('cursor');
  });

  it('retains the last complete inventory if a later page of a refresh fails', async () => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(page([sandbox({ id: 'new-sandbox' })], 'next', 2))
      .mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh failed.');
    expect(screen.getByText('sandbox-one')).toBeInTheDocument();
    expect(screen.queryByText('new-sandbox')).not.toBeInTheDocument();
    expect(screen.queryByText('No running sandboxes.')).not.toBeInTheDocument();
  });

  it.each([401, 403])('clears previously loaded inventory on HTTP %i', async (status) => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    vi.mocked(getAdminSandboxes).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', status));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    expect(screen.queryByText('owner-one')).not.toBeInTheDocument();
  });

  it('polls every thirty seconds and stops polling on unmount', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const view = render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
  });

  it('does not start overlapping polling requests while a complete inventory is loading', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const pending = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes).mockReturnValueOnce(pending.promise);
    render(<AdminSandboxesPage />);
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
    await act(async () => pending.resolve(page()));
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
  });

  it('clears inventory immediately when the current user loses admin permission', async () => {
    const view = render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    auth.access = { allowed: true, permissions: ['agent.use'] };
    view.rerender(<AdminSandboxesPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
  });

  it('aborts an old user’s read and ignores its late result after a user switch', async () => {
    const oldRead = deferred<InventoryPage>();
    const newRead = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise);
    const view = render(<AdminSandboxesPage />);
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(getAdminSandboxes).mock.calls[0][1];
    auth.id = 'admin-two';
    view.rerender(<AdminSandboxesPage />);
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(2));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => newRead.resolve(page([sandbox({ id: 'new-user-sandbox' })])));
    await screen.findByText('new-user-sandbox');
    await act(async () => oldRead.resolve(page()));
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    expect(screen.getByText('new-user-sandbox')).toBeInTheDocument();
  });

  it('does not expose the previous user’s snapshot while a new user’s read is pending', async () => {
    const view = render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    const newRead = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes).mockReturnValueOnce(newRead.promise);
    auth.id = 'admin-two';
    view.rerender(<AdminSandboxesPage />);
    expect(screen.getByText('Loading running sandboxes…')).toBeInTheDocument();
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    await act(async () => newRead.resolve(page([])));
    await screen.findByText('No running sandboxes.');
  });

  it('expands container resource and lifecycle stats without turning unknown values into zero', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({
          resource_usage: usage({
            cpu_cores: 0,
            memory_bytes: 0,
            pids: 0,
            net_rx_bytes: 0,
            net_tx_bytes: null,
            io_read_bytes: null,
            io_write_bytes: 0,
          }),
          resident_slot_capacity: 6,
          active_operation_count: 0,
          snapshot_state: 'ready',
          terminal_count: 0,
        }),
      ]),
    );
    render(<AdminSandboxesPage />);
    const button = await screen.findByRole('button', { name: 'Show details for Fix login' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('Stats for Fix login')).not.toBeInTheDocument();
    const row = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    expect(row).toHaveTextContent('0.00 cores');
    expect(row).toHaveTextContent('0 B');
    expect(within(row).getByRole('meter', { name: 'CPU quota for Fix login' })).toHaveAttribute(
      'max',
      '2',
    );
    expect(within(row).getByRole('meter', { name: 'Memory limit for Fix login' })).toHaveAttribute(
      'max',
      String(4 * 1024 ** 3),
    );
    fireEvent.click(button);
    const stats = screen.getByLabelText('Stats for Fix login');
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(statValue('Network received', stats)).toHaveTextContent('0 B');
    expect(statValue('Network sent', stats)).toHaveTextContent('Unavailable');
    expect(statValue('Disk read', stats)).toHaveTextContent('Unavailable');
    expect(statValue('Disk written', stats)).toHaveTextContent('0 B');
    expect(statValue('Processes', stats)).toHaveTextContent('0');
    expect(statValue('Sessions / capacity', stats)).toHaveTextContent('3 / 6');
    expect(statValue('Terminals', stats)).toHaveTextContent('0');
    expect(statValue('Active operations', stats)).toHaveTextContent('0');
    expect(statValue('Snapshot', stats)).toHaveTextContent('ready');
    expect(statValue('Configured limits', stats)).toHaveTextContent(
      '2 CPU · 4g memory · No disk quota',
    );
    fireEvent.click(button);
    expect(screen.queryByLabelText('Stats for Fix login')).not.toBeInTheDocument();
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it.each([
    [20480, '20 GiB disk'],
    [1500, '1500 MiB disk'],
    [0, 'No disk quota'],
  ] as const)(
    'shows a retained disk limit of %s MiB in configured limits',
    async (quota, label) => {
      vi.mocked(getAdminSandboxes).mockResolvedValue(page([sandbox({ disk_limit_mib: quota })]));
      render(<AdminSandboxesPage />);
      fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
      const stats = screen.getByLabelText('Stats for Fix login');
      expect(statValue('Configured limits', stats)).toHaveTextContent(
        `2 CPU · 4g memory · ${label}`,
      );
    },
  );

  it('shows fleet resource totals only for fresh measurements and states their coverage', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({ resource_usage: usage() }),
        sandbox({
          id: 'sandbox-two',
          title: 'Old sample',
          host: 'host-two',
          resource_usage: usage({ cpu_cores: 99, memory_bytes: 99 * 1024 ** 3, stale: true }),
        }),
        sandbox({ id: 'sandbox-three', title: 'Old host' }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    expect(statValue('Measured CPU')).toHaveTextContent('0.25 cores');
    expect(statValue('Measured memory')).toHaveTextContent('1 MiB');
    expect(screen.getAllByText('1 / 3 sandboxes · fresh samples only')).toHaveLength(2);
    expect(statValue('Running sandboxes')).toHaveTextContent('3');
    expect(statValue('Active turns')).toHaveTextContent('3');
    expect(statValue('Sessions')).toHaveTextContent('9');
    expect(statValue('Hosts')).toHaveTextContent('2');
    const oldRow = screen.getByRole('link', { name: 'Old sample' }).closest('tr')!;
    expect(oldRow).toHaveTextContent('Stale sample');
  });

  it('preserves resource sample age from each page while waiting for the complete inventory', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(observedAt));
    const rest = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(
        page([sandbox({ resource_usage: usage({ age_seconds: 0, cpu_cores: 8 }) })], 'next', 2),
      )
      .mockReturnValueOnce(rest.promise);
    render(<AdminSandboxesPage />);
    await act(async () => {});
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(100_000));
    const laterObservation = '2026-10-10T10:01:40Z';
    await act(async () =>
      rest.resolve({
        ...page([
          sandbox({
            id: 'sandbox-two',
            title: 'Second session',
            host_last_heartbeat_at: laterObservation,
            resource_usage: usage({ age_seconds: 0, cpu_cores: 0, memory_bytes: 0 }),
          }),
        ]),
        observed_at: laterObservation,
      }),
    );
    expect(statValue('Measured CPU')).toHaveTextContent('0.00 cores');
    expect(statValue('Measured memory')).toHaveTextContent('0 B');
    expect(screen.getAllByText('1 / 2 sandboxes · fresh samples only')).toHaveLength(2);
    const firstRow = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    const secondRow = screen.getByRole('link', { name: 'Second session' }).closest('tr')!;
    expect(firstRow).toHaveTextContent('Stale sample');
    expect(secondRow).not.toHaveTextContent('Stale sample');
  });

  it('keeps older host responses usable and displays missing telemetry as unavailable', async () => {
    render(<AdminSandboxesPage />);
    const button = await screen.findByRole('button', { name: 'Show details for Fix login' });
    expect(statValue('Measured CPU')).toHaveTextContent('Unavailable');
    expect(statValue('Measured memory')).toHaveTextContent('Unavailable');
    fireEvent.click(button);
    const stats = screen.getByLabelText('Stats for Fix login');
    for (const label of [
      'Network received',
      'Network sent',
      'Disk read',
      'Disk written',
      'Processes',
      'Sample age',
    ]) {
      expect(statValue(label, stats)).toHaveTextContent('Unavailable');
    }
    expect(statValue('Terminals', stats)).toHaveTextContent('Unknown');
    expect(statValue('Active operations', stats)).toHaveTextContent('Unavailable');
    expect(statValue('Snapshot', stats)).toHaveTextContent('Unavailable');
  });

  it('marks an older terminal count stale even when the host heartbeat and resource sample are fresh', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({
          terminal_count: 0,
          terminal_count_observed_at: '2026-10-10T09:57:00Z',
          resource_usage: usage(),
        }),
      ]),
    );
    render(<AdminSandboxesPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Show details for Fix login' }));
    const stats = screen.getByRole('region', { name: 'Stats for Fix login' });
    expect(statValue('Terminals', stats)).toHaveTextContent('0 · Stale count');
    expect(statValue('Sample age', stats)).toHaveTextContent('Fresh');
    expect(statValue('Measured CPU')).toHaveTextContent('0.25 cores');
  });

  it('ages retained resource measurements out of totals while a refresh fails', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'performance', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(observedAt));
    vi.mocked(getAdminSandboxes).mockResolvedValueOnce(
      page([sandbox({ resource_usage: usage({ age_seconds: 89 }) })]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    expect(statValue('Measured CPU')).toHaveTextContent('0.25 cores');
    vi.mocked(getAdminSandboxes).mockRejectedValue(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await act(async () => {});
    expect(screen.getByRole('alert')).toHaveTextContent('Refresh failed.');
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(screen.getByText('sandbox-one')).toBeInTheDocument();
    expect(statValue('Measured CPU')).toHaveTextContent('Unavailable');
    expect(statValue('Measured memory')).toHaveTextContent('Unavailable');
    expect(screen.getAllByText('0 / 1 sandboxes · fresh samples only')).toHaveLength(2);
    const row = screen.getByRole('link', { name: 'Fix login' }).closest('tr')!;
    expect(row).toHaveTextContent('Stale sample');
    expect(row).toHaveTextContent('0.25 cores');
  });
});

describe('sandbox inventory controls', () => {
  it('starts in sandbox ID order and toggles accessible session-name sorting', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({ id: 'sandbox-z', title: 'Alpha session' }),
        sandbox({ id: 'sandbox-a', title: 'Zulu session' }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-a');
    expect(inventoryTitles()).toEqual(['Zulu session', 'Alpha session']);

    const sort = screen.getByRole('button', { name: 'Sort by Sandbox / session' });
    fireEvent.click(sort);
    expect(inventoryTitles()).toEqual(['Alpha session', 'Zulu session']);
    expect(sort.closest('th')).toHaveAttribute('aria-sort', 'ascending');
    fireEvent.click(sort);
    expect(inventoryTitles()).toEqual(['Zulu session', 'Alpha session']);
    expect(sort.closest('th')).toHaveAttribute('aria-sort', 'descending');
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  });

  it.each<{
    column: string;
    low: Partial<AdminSandbox>;
    high: Partial<AdminSandbox>;
    unknown?: Partial<AdminSandbox>;
  }>([
    {
      column: 'Activity / agent',
      low: { active_attempt_count: 2 },
      high: { active_attempt_count: 10 },
    },
    {
      column: 'CPU',
      low: { resource_usage: usage({ cpu_cores: 2 }) },
      high: { resource_usage: usage({ cpu_cores: 10 }) },
      unknown: { resource_usage: usage({ cpu_cores: null }) },
    },
    {
      column: 'Memory',
      low: { resource_usage: usage({ memory_bytes: 1024 }) },
      high: { resource_usage: usage({ memory_bytes: 1024 ** 2 }) },
      unknown: { resource_usage: usage({ memory_bytes: null }) },
    },
    {
      column: 'Sessions',
      low: { member_count: 2 },
      high: { member_count: 10 },
    },
    {
      column: 'Last activity',
      low: { last_activity_at: '2026-10-10T09:00:00Z' },
      high: { last_activity_at: '2026-10-10T09:59:00Z' },
      unknown: { last_activity_at: 'invalid-date' },
    },
    {
      column: 'Host heartbeat',
      low: { host_last_heartbeat_at: '2026-10-10T09:00:00Z' },
      high: { host_last_heartbeat_at: observedAt },
      unknown: { host_last_heartbeat_at: null },
    },
  ])(
    'sorts $column numerically and leaves unknown values last in either direction',
    async (test) => {
      const rows = [
        sandbox({ id: 'sandbox-low', title: 'Lower value', ...test.low }),
        sandbox({ id: 'sandbox-high', title: 'Higher value', ...test.high }),
        ...(test.unknown
          ? [sandbox({ id: 'sandbox-unknown', title: 'Unknown value', ...test.unknown })]
          : []),
      ];
      vi.mocked(getAdminSandboxes).mockResolvedValue(page(rows));
      render(<AdminSandboxesPage />);
      await screen.findByText('sandbox-low');
      const sort = screen.getByRole('button', { name: `Sort by ${test.column}` });
      const tail = test.unknown ? ['Unknown value'] : [];
      fireEvent.click(sort);
      expect(inventoryTitles()).toEqual(['Higher value', 'Lower value', ...tail]);
      expect(sort.closest('th')).toHaveAttribute('aria-sort', 'descending');
      fireEvent.click(sort);
      expect(inventoryTitles()).toEqual(['Lower value', 'Higher value', ...tail]);
      expect(sort.closest('th')).toHaveAttribute('aria-sort', 'ascending');
      expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
    },
  );

  it('sorts owners by their IDs with sandbox IDs breaking ties', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({ id: 'sandbox-z', title: 'Second owner', user_id: 'owner-z' }),
        sandbox({ id: 'sandbox-b', title: 'Second sandbox', user_id: 'owner-a' }),
        sandbox({ id: 'sandbox-a', title: 'First sandbox', user_id: 'owner-a' }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-a');
    const sort = screen.getByRole('button', { name: 'Sort by Owner / host' });
    fireEvent.click(sort);
    expect(inventoryTitles()).toEqual(['First sandbox', 'Second sandbox', 'Second owner']);
    expect(sort.closest('th')).toHaveAttribute('aria-sort', 'ascending');
    fireEvent.click(sort);
    expect(inventoryTitles()).toEqual(['Second owner', 'First sandbox', 'Second sandbox']);
    expect(sort.closest('th')).toHaveAttribute('aria-sort', 'descending');
  });

  it.each([
    '  FIX LOGIN  ',
    'sandbox-one',
    'org/repo-one',
    'owner-one',
    'host-one',
    'container-one-id',
    'container-one',
    'codex',
  ])('searches sandbox metadata locally for %j', async (query) => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox(),
        sandbox({
          id: 'sandbox-two',
          title: 'Another session',
          repo: 'org/repo-two',
          user_id: 'owner-two',
          host: 'host-two',
          container_id: 'container-two-id',
          container_name: 'container-two',
          runtime_id: 'claude',
        }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sandboxes' }), {
      target: { value: query },
    });
    expect(inventoryTitles()).toEqual(['Fix login']);
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  });

  it('combines host, activity, and search filters and clears them without rereading the fleet', async () => {
    vi.mocked(getAdminSandboxes).mockResolvedValue(
      page([
        sandbox({ id: 'sandbox-a', title: 'Alpha', host: 'host-one' }),
        sandbox({
          id: 'sandbox-b',
          title: 'Bravo',
          host: 'host-one',
          active_attempt_count: 0,
        }),
        sandbox({
          id: 'sandbox-c',
          title: 'Charlie',
          host: 'host-two',
          active_attempt_count: 0,
        }),
        sandbox({ id: 'sandbox-d', title: 'Delta', host: 'host-two' }),
      ]),
    );
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-a');
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox host' }), {
      target: { value: 'host-one' },
    });
    expect(inventoryTitles()).toEqual(['Alpha', 'Bravo']);
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox activity' }), {
      target: { value: 'idle' },
    });
    expect(inventoryTitles()).toEqual(['Bravo']);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sandboxes' }), {
      target: { value: ' BRAVO ' },
    });
    expect(inventoryTitles()).toEqual(['Bravo']);
    expect(statValue('Running sandboxes')).toHaveTextContent('4');
    expect(statValue('Active turns')).toHaveTextContent('2');

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByRole('searchbox', { name: 'Search sandboxes' })).toHaveValue('');
    expect(
      (screen.getByRole('combobox', { name: 'Sandbox host' }) as HTMLSelectElement).selectedIndex,
    ).toBe(0);
    expect(
      (screen.getByRole('combobox', { name: 'Sandbox activity' }) as HTMLSelectElement)
        .selectedIndex,
    ).toBe(0);
    expect(inventoryTitles()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Delta']);
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  });

  it.each<AdminSandbox['state']>([
    'provisioning',
    'starting',
    'stopping',
    'stopped',
    'stopped_dirty',
    'lost',
  ])('loads the selected %s lifecycle state from the server', async (state) => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    vi.mocked(getAdminSandboxes).mockResolvedValueOnce(
      page([sandbox({ id: 'filtered-sandbox', state })]),
    );
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox state' }), {
      target: { value: state },
    });
    await screen.findByText('filtered-sandbox');
    expect(getAdminSandboxes).toHaveBeenLastCalledWith(
      { limit: 200, state },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('combobox', { name: 'Sandbox state' })).toHaveValue(state);
  });

  it('requests removed sandboxes in the current state and ignores a superseded refresh', async () => {
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    const stopped = sandbox({ state: 'stopped', active_attempt_count: 0 });
    const oldRead = deferred<InventoryPage>();
    const includeRemovedRead = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes)
      .mockResolvedValueOnce(page([stopped]))
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(includeRemovedRead.promise)
      .mockResolvedValue(page([stopped]));
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox state' }), {
      target: { value: 'all' },
    });
    await screen.findByText('sandbox-one');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(getAdminSandboxes).toHaveBeenCalledTimes(3));
    const oldSignal = vi.mocked(getAdminSandboxes).mock.calls[2][1];

    const includeRemoved = screen.getByRole('checkbox', { name: 'Include removed sandboxes' });
    fireEvent.click(includeRemoved);
    expect(screen.queryByText('sandbox-one')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(getAdminSandboxes).toHaveBeenLastCalledWith(
        { limit: 200, state: 'all', include_archived: true },
        expect.any(AbortSignal),
      ),
    );
    expect(oldSignal?.aborted).toBe(true);
    await act(async () =>
      includeRemovedRead.resolve(
        page([
          sandbox({
            id: 'removed-sandbox',
            title: 'Removed session',
            state: 'stopped',
            archived_at: observedAt,
            active_attempt_count: 0,
          }),
        ]),
      ),
    );
    expect(screen.getByText('removed-sandbox')).toBeInTheDocument();
    await act(async () => oldRead.resolve(page([sandbox({ id: 'late-sandbox' })])));
    expect(screen.queryByText('late-sandbox')).not.toBeInTheDocument();
    expect(screen.getByText('removed-sandbox')).toBeInTheDocument();

    fireEvent.click(includeRemoved);
    await screen.findByText('sandbox-one');
    expect(getAdminSandboxes).toHaveBeenLastCalledWith(
      { limit: 200, state: 'all' },
      expect.any(AbortSignal),
    );
    expect(screen.queryByText('removed-sandbox')).not.toBeInTheDocument();
    expect(includeRemoved).not.toBeChecked();
  });

  it('retains search, host, activity, and chosen sort when manually refreshing measurements', async () => {
    const first = sandbox({
      id: 'sandbox-a',
      title: 'Target Alpha',
      active_attempt_count: 0,
      resource_usage: usage({ cpu_cores: 2 }),
    });
    const second = sandbox({
      id: 'sandbox-b',
      title: 'Target Bravo',
      active_attempt_count: 0,
      resource_usage: usage({ cpu_cores: 10 }),
    });
    const excluded = sandbox({ id: 'sandbox-c', title: 'Another session', host: 'host-two' });
    vi.mocked(getAdminSandboxes).mockResolvedValue(page([first, second, excluded]));
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-a');
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sandboxes' }), {
      target: { value: 'target' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox host' }), {
      target: { value: 'host-one' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sandbox activity' }), {
      target: { value: 'idle' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort sandboxes' }), {
      target: { value: 'cpu' },
    });
    expect(inventoryTitles()).toEqual(['Target Bravo', 'Target Alpha']);

    vi.mocked(getAdminSandboxes).mockResolvedValueOnce(
      page([
        { ...first, resource_usage: usage({ cpu_cores: 11 }) },
        { ...second, resource_usage: usage({ cpu_cores: 1 }) },
        excluded,
      ]),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(inventoryTitles()).toEqual(['Target Alpha', 'Target Bravo']));
    expect(screen.getByRole('searchbox', { name: 'Search sandboxes' })).toHaveValue('target');
    expect(screen.getByRole('combobox', { name: 'Sandbox host' })).toHaveValue('host-one');
    expect(screen.getByRole('combobox', { name: 'Sandbox activity' })).toHaveValue('idle');
    expect(screen.getByRole('combobox', { name: 'Sort sandboxes' })).toHaveValue('cpu');
    expect(screen.getByRole('button', { name: 'Sort by CPU' }).closest('th')).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Sort ascending' }));
    expect(inventoryTitles()).toEqual(['Target Bravo', 'Target Alpha']);
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
  });

  it('pauses automatic polling while retaining manual refresh and resumes thirty-second polling', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    render(<AdminSandboxesPage />);
    await screen.findByText('sandbox-one');
    const autoRefresh = screen.getByRole('checkbox', { name: 'Auto-refresh' });
    expect(autoRefresh).toBeChecked();
    fireEvent.click(autoRefresh);
    expect(autoRefresh).not.toBeChecked();
    await act(async () => vi.advanceTimersByTimeAsync(90_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await act(async () => {});
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    expect(autoRefresh).not.toBeChecked();
    fireEvent.click(autoRefresh);
    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(3);
  });

  it('keeps a pending inventory read valid when automatic polling is paused', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const pending = deferred<InventoryPage>();
    vi.mocked(getAdminSandboxes).mockReturnValueOnce(pending.promise);
    render(<AdminSandboxesPage />);
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
    const signal = vi.mocked(getAdminSandboxes).mock.calls[0][1];
    fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-refresh' }));
    expect(signal?.aborted).toBe(false);
    await act(async () => pending.resolve(page()));
    expect(screen.getByText('sandbox-one')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  });
});
