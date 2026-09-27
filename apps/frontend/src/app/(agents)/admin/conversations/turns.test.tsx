// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAccessApi } from '@/lib/api/session';
import type {
  AdminRun,
  AdminRunsPage,
  ResourceMetricBriefApi,
  RunResourceSummaryApi,
} from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

// The turn level of the Sessions tab, opened the way a link opens it. The
// applied window comes from the URL, so a usage deep link can open this list
// on exactly the cohort it was aggregated from. The store notifies its
// subscribers, which is how a real URL change re-renders the mounted page.
const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
const search = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    value: '',
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => search.value,
    set(next: string) {
      search.value = next;
      for (const listener of listeners) listener();
    },
  };
});
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'admin', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('next/navigation', () => ({
  useParams: () => ({}),
  useSearchParams: () => {
    const query = useSyncExternalStore(search.subscribe, search.read);
    return new URLSearchParams(query ? `level=turns&${query}` : 'level=turns');
  },
}));
vi.mock('@/lib/api/adminRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminRuns')>()),
  getAdminRuns: vi.fn(),
}));
vi.mock('@/lib/api/adminConversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminConversations')>()),
  getAdminConversations: vi.fn(),
}));

import { getAdminRuns } from '@/lib/api/adminRuns';
import { getAdminConversations } from '@/lib/api/adminConversations';
import SessionsPage from './page';
import {
  collectingSentence,
  formatCpuSeconds,
  formatMemoryBytes,
  UNKNOWN_REASON_SENTENCE,
} from '../runs/presentTurnResources';

function run(overrides: Partial<AdminRun> = {}): AdminRun {
  return {
    id: 'job-1',
    title: 'Inspect parser',
    thread_id: 'thread-1',
    turn_no: 2,
    user_id: 'other-user',
    repo: 'org/repo',
    runtime: 'claude-code',
    model: 'model-a',
    reasoning_effort: null,
    execution_profile: 'untrusted-kata-v1',
    state: 'succeeded',
    created_at: '2026-09-10T08:00:00Z',
    updated_at: '2026-09-10T08:02:00Z',
    hosts: ['host-a'],
    tool_calls: 1,
    ...overrides,
  };
}

function page(runs = [run()], next: string | null = null): AdminRunsPage {
  return { runs, next_cursor: next, since: '2026-09-03T08:00:00Z', until: '2026-09-10T08:00:00Z' };
}

/** The compact per-turn resource projection the Turns list reads. */
function runSummary(overrides: Partial<RunResourceSummaryApi> = {}): RunResourceSummaryApi {
  return {
    cpu_usec: 80_000_000,
    memory_peak_bytes: 1_288_490_189,
    state: 'final',
    observed_through_ms: 130_000,
    cpu: { status: 'available', reason_codes: [] },
    memory: { status: 'available', reason_codes: [] },
    storage_io: { status: 'unavailable', reason_codes: ['filesystem_attribution_unverified'] },
    attempts_measured: 1,
    attempts_total: 1,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/**
 * The list row whose accessible name starts with the run's link text.
 *
 * Queried this way because a resource cell must be asserted inside the row it
 * belongs to: "80 CPU-s appears somewhere" would also pass for a stale cell.
 */
function row(runId: string): HTMLElement {
  return screen.getByRole('row', {
    name: (name: string) => name.startsWith(runId),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  vi.mocked(getAdminRuns).mockResolvedValue(page());
});
afterEach(cleanup);

describe('turn level', () => {
  it('lists turns, not sessions, and reads nothing at the session level', async () => {
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByRole('button', { name: 'Turns', pressed: true })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sessions', pressed: false })).toBeInTheDocument();
    expect(getAdminConversations).not.toHaveBeenCalled();
  });

  it('displays physical hosts and counts machines instead of runner deployments', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([run({ hosts: ['gpu-box', 'gpu-box-kata'], physical_hosts: ['gpu-box'] })]),
    );
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('gpu-box')).toBeInTheDocument();
    expect(screen.queryByText('gpu-box-kata')).not.toBeInTheDocument();
    expect(screen.queryByText('+1')).not.toBeInTheDocument();
  });

  it('keeps legacy runner names when the API has no physical host field', async () => {
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('host-a')).toBeInTheDocument();
  });

  it('shows the number of additional physical machines', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({ hosts: ['runner-a', 'runner-b', 'runner-c'], physical_hosts: ['box-a', 'box-b'] }),
      ]),
    );
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('box-a, box-b')).toBeInTheDocument();
    expect(screen.getByText('+1')).toBeInTheDocument();
  });

  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not fetch cross-user data without a current admin permission: %j', (access) => {
    auth.access = access;
    render(<SessionsPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('defaults to an anchored seven-day window and exposes working admin sections', async () => {
    render(<SessionsPage />);
    expect(await screen.findByRole('link', { name: 'job-1' })).toHaveAttribute(
      'href',
      '/admin/runs/job-1',
    );
    expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Inspect parser')).not.toBeInTheDocument();
    const filters = vi.mocked(getAdminRuns).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
    expect(screen.getByLabelText('Turn created')).toHaveValue('7');
    expect(screen.getByLabelText('Turn result')).toHaveValue('');
    expect(
      within(screen.getByLabelText('Turn result'))
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['All turn results', 'queued', 'running', 'succeeded', 'failed', 'cancelled']);
    const controls = within(screen.getByRole('group', { name: 'Filters' }));
    expect(controls.getAllByRole('combobox')).toHaveLength(2);
    expect(controls.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
    expect(controls.queryByRole('textbox')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /More filters|Apply filters|Clear filters/ }),
    ).not.toBeInTheDocument();
    for (const label of ['User ID', 'Repository', 'Agent', 'Model', 'Host']) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(nav).getByRole('link', { name: 'Sessions' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).getByRole('link', { name: 'Servers' })).toHaveAttribute(
      'href',
      '/admin/hosts',
    );
    expect(within(nav).getByRole('link', { name: 'Usage' })).toHaveAttribute(
      'href',
      '/admin/usage',
    );
  });

  it('honours an absolute window and identities from a usage deep link', async () => {
    search.value =
      'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&model=model-a&user_id=owner-one';
    vi.mocked(getAdminRuns).mockResolvedValue(page());
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    // The linked window replaces the seven-day default, and the identities the
    // usage row was grouped by travel with it.
    expect(vi.mocked(getAdminRuns).mock.calls[0][0]).toMatchObject({
      since: '2026-09-01T00:00:00.000Z',
      until: '2026-09-08T00:00:00.000Z',
      model: 'model-a',
      user_id: 'owner-one',
    });
    // The control names the window that is applied: a week that ended a month
    // ago is Custom, not "Last 7 days".
    expect(screen.getByLabelText('Turn created')).toHaveValue('custom');
    expect(screen.getByText(/Filters from the linked selection are preserved/)).toBeInTheDocument();
  });

  it('keeps a linked window on Refresh instead of drifting with the clock', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z';
      vi.mocked(getAdminRuns).mockResolvedValue(page());
      render(<SessionsPage />);
      await screen.findByRole('link', { name: 'job-1' });
      vi.setSystemTime(new Date('2026-09-10T09:00:00Z'));
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
      expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toMatchObject({
        since: '2026-09-01T00:00:00.000Z',
        until: '2026-09-08T00:00:00.000Z',
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('refreshes a window that has aged past its preset', async () => {
    // The Created control reads Custom once the window is older than a preset
    // tolerance, so the refresh length has to come from the window on screen.
    // Reading it from the control's value produced an invalid date.
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      vi.mocked(getAdminRuns).mockResolvedValue(page());
      render(<SessionsPage />);
      await screen.findByRole('link', { name: 'job-1' });

      vi.setSystemTime(new Date('2026-09-10T08:06:00Z'));
      fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
      expect(screen.getByLabelText('Turn created')).toHaveValue('custom');

      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(3));
      const filters = vi.mocked(getAdminRuns).mock.lastCall![0];
      // The same seven-day window, re-anchored at the current time.
      expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
      expect(filters.until).toBe('2026-09-10T08:06:00.000Z');
      expect(filters.state).toBe('failed');
    } finally {
      vi.useRealTimers();
    }
  });

  it('follows a URL that changes while the list stays mounted', async () => {
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(vi.mocked(getAdminRuns).mock.calls[0][0].model).toBeUndefined();

    search.set('since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&model=model-a');
    await act(async () => {});
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminRuns).mock.calls[1][0]).toMatchObject({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      model: 'model-a',
    });
  });

  it('reports an unusable link instead of answering for every run', async () => {
    // An inverted window is a broken link, not an empty selection. Falling back
    // to the default window would answer a question nobody asked.
    search.value = 'since=2026-09-08T00:00:00.000Z&until=2026-09-01T00:00:00.000Z';
    render(<SessionsPage />);
    await screen.findByRole('alert');
    expect(screen.getByText(/empty or inverted window/)).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('reports a link naming a filter this list cannot express', async () => {
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&foo=bar';
    render(<SessionsPage />);
    await screen.findByRole('alert');
    expect(screen.getByText(/Unsupported filter: foo/)).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('keeps a one-day linked window when Status changes', async () => {
    // The Created control names the window that is applied, so changing a
    // non-time filter cannot re-derive seven days from a stale preset.
    search.value = 'since=2026-09-09T00:00:00.000Z&until=2026-09-10T00:00:00.000Z';
    vi.mocked(getAdminRuns).mockResolvedValue(page());
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByLabelText('Turn created')).toHaveValue('custom');
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toMatchObject({
      since: '2026-09-09T00:00:00.000Z',
      until: '2026-09-10T00:00:00.000Z',
      state: 'failed',
    });
  });

  it('applies filters and paginates with the same window and filter scope', async () => {
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'cursor-2'))
      .mockResolvedValueOnce(page([run({ id: 'job-2', title: 'Second run' })]));
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
    await screen.findByRole('link', { name: 'job-2' });
    expect(getAdminRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({
        state: 'failed',
        cursor: 'cursor-2',
        since: page().since,
        until: page().until,
      }),
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'job-1' })).toBeInTheDocument();
    expect(screen.queryByText('Second run')).not.toBeInTheDocument();
  });

  it('keeps the queued filter and badge across pages', async () => {
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run({ state: 'queued' })], 'group-cursor'))
      .mockResolvedValueOnce(page([run({ id: 'job-2', state: 'queued' })]));
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'queued' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
    await screen.findByRole('link', { name: 'job-2' });

    expect(vi.mocked(getAdminRuns).mock.calls[1][0]).toEqual(
      expect.objectContaining({ state: 'queued' }),
    );
    expect(getAdminRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({
        state: 'queued',
        cursor: 'group-cursor',
        since: page().since,
        until: page().until,
      }),
      expect.any(AbortSignal),
    );
    const table = within(screen.getByRole('table'));
    expect(table.getAllByText('queued')).toHaveLength(2);
  });

  it('applies Created and Status immediately and replaces previously paged rows', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'cursor-2'))
      .mockResolvedValueOnce(page([run({ id: 'job-2' })]))
      .mockResolvedValueOnce(page([run({ id: 'job-30-days' })]))
      .mockResolvedValueOnce(page([run({ id: 'job-failed', state: 'failed' })]));
    render(<SessionsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
    await screen.findByRole('link', { name: 'job-2' });

    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await screen.findByRole('link', { name: 'job-30-days' });
    const timeFilters = vi.mocked(getAdminRuns).mock.lastCall![0];
    expect(Date.parse(timeFilters.until!) - Date.parse(timeFilters.since!)).toBe(30 * 86_400_000);
    expect(timeFilters.cursor).toBeUndefined();
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'job-2' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    await screen.findByRole('link', { name: 'job-failed' });
    const statusFilters = vi.mocked(getAdminRuns).mock.lastCall![0];
    expect(statusFilters.state).toBe('failed');
    expect(Date.parse(statusFilters.until!) - Date.parse(statusFilters.since!)).toBe(
      30 * 86_400_000,
    );
    expect(statusFilters.cursor).toBeUndefined();
    expect(screen.queryByRole('link', { name: 'job-30-days' })).not.toBeInTheDocument();
    expect(getAdminRuns).toHaveBeenCalledTimes(4);
  });

  it('refreshes the time window while preserving Status and replacing previously paged rows', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      vi.mocked(getAdminRuns)
        .mockResolvedValueOnce(page())
        .mockResolvedValueOnce(page([run({ state: 'failed' })], 'cursor-2'))
        .mockResolvedValueOnce(page([run({ id: 'job-2', state: 'failed' })]))
        .mockResolvedValueOnce(page([run({ id: 'refreshed-run', state: 'failed' })]));
      render(<SessionsPage />);
      await screen.findByRole('link', { name: 'job-1' });
      fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
      fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
      await screen.findByRole('link', { name: 'job-2' });

      vi.setSystemTime(new Date('2026-09-10T09:00:00Z'));
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await screen.findByRole('link', { name: 'refreshed-run' });
      const filters = vi.mocked(getAdminRuns).mock.lastCall![0];
      expect(filters.since).toBe('2026-09-03T09:00:00.000Z');
      expect(filters.until).toBe('2026-09-10T09:00:00.000Z');
      expect(filters.state).toBe('failed');
      expect(filters.cursor).toBeUndefined();
      expect(screen.getByLabelText('Turn result')).toHaveValue('failed');
      expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'job-2' })).not.toBeInTheDocument();
      expect(getAdminRuns).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('ignores an old response when the filters change', async () => {
    const pending = deferred<AdminRunsPage>();
    vi.mocked(getAdminRuns)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([run({ id: 'current-match' })]));
    render(<SessionsPage />);
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await screen.findByRole('link', { name: 'current-match' });
    await act(async () => pending.resolve(page([run({ id: 'stale-match' })])));
    expect(screen.queryByRole('link', { name: 'stale-match' })).not.toBeInTheDocument();
    expect(vi.mocked(getAdminRuns).mock.calls[0][1]?.aborted).toBe(true);
  });

  it('ignores a late pagination response after Status changes the filter scope', async () => {
    const pending = deferred<AdminRunsPage>();
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'old-cursor'))
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([run({ id: 'current-match', state: 'failed' })]));
    render(<SessionsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
    fireEvent.change(screen.getByLabelText('Turn result'), { target: { value: 'failed' } });
    await screen.findByRole('link', { name: 'current-match' });
    expect(vi.mocked(getAdminRuns).mock.calls[1][1]?.aborted).toBe(true);
    expect(vi.mocked(getAdminRuns).mock.lastCall![0].cursor).toBeUndefined();

    await act(async () =>
      pending.resolve(page([run({ id: 'stale-match' })], 'another-old-cursor')),
    );
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'stale-match' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more turns' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'current-match' })).toBeInTheDocument();
  });

  it('clears retained cross-user rows when a later page rejects revoked access', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'more'))
      .mockRejectedValueOnce(new APIError('FORBIDDEN', 'Admin permission revoked', 403));
    render(<SessionsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more turns' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
  });
});

describe('turn level resource cells', () => {
  // The list asserts a cell's text belongs to the run on screen, never that
  // some text appeared: a cell left over from the previous selection is the
  // failure this column can actually have.
  it('says a run with no whole-turn summary has none, rather than showing a zero', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(page([run()]));
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getAllByText('Not collected')).toHaveLength(3);
    expect(within(row('job-1')).queryByText('0 B')).not.toBeInTheDocument();
    expect(within(row('job-1')).queryByText('0 CPU-s')).not.toBeInTheDocument();
  });

  it('shows CPU and the sampled memory peak for a measured run', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(page([run({ resource_summary: runSummary() })]));
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getByText(`${formatCpuSeconds(80_000_000)}`)).toBeInTheDocument();
    expect(
      within(row('job-1')).getByText(`${formatMemoryBytes(1_288_490_189)} \u00b7 sampled`),
    ).toBeInTheDocument();
    // An unsupported I/O metric does not make the row read as a zero.
    expect(screen.queryByText(/^0 B$/)).not.toBeInTheDocument();
  });

  it('never leaves the previous run\u2019s resource cell beside a new selection', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run({ resource_summary: runSummary() })]))
      .mockResolvedValueOnce(page([run({ resource_summary: null })]));
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getByText('80 CPU-s')).toBeInTheDocument();

    // A filter change replaces the page; the old numbers must go with it.
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(within(row('job-1')).queryByText('80 CPU-s')).not.toBeInTheDocument(),
    );
    expect(within(row('job-1')).getAllByText('Not collected')).toHaveLength(3);
  });

  it('renders a partial CPU measurement as a lower bound and a partial peak as observed', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            state: 'interrupted',
            observed_through_ms: 130_000,
            cpu: { status: 'partial', reason_codes: ['ring_loss'] },
            memory: { status: 'partial', reason_codes: ['pss_unreadable'] },
            attempts_measured: 3,
            attempts_total: 5,
          }),
        }),
      ]),
    );
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    const cpu = within(row('job-1')).getByText(/\u2265 80 CPU-s/);
    expect(cpu).toBeInTheDocument();
    expect(cpu.textContent).toContain('collection was interrupted');
    const memory = within(row('job-1')).getByText(/1.2 GiB/);
    expect(memory.textContent).toContain('observed');
    expect(memory.textContent).not.toContain('\u2265');
    // A total over three of five executions is not the run's total.
    const state = within(row('job-1')).getByText('Interrupted');
    expect(state.getAttribute('title')).toContain('3 of 5 executions measured');
  });

  it('states when collection is still running rather than showing a final total', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            state: 'collecting',
            observed_through_ms: 90_000,
            memory: { status: 'partial', reason_codes: ['sampling_over_budget'] },
          }),
        }),
      ]),
    );
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    const state = within(row('job-1')).getByText('Collecting');
    // The string handed to title is checked separately: jsdom renders the
    // attribute, and the sentence must name how far the window actually got.
    expect(state.getAttribute('title')).toBe(collectingSentence(90_000));
    expect(state.getAttribute('title')).toContain('not the turn\u2019s total');
  });

  it('gives an unknown reason code the generic sentence, never the code', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            cpu: {
              status: 'partial',
              reason_codes: [
                'uid=1001 lease_token=deadbeef',
              ] as unknown as ResourceMetricBriefApi['reason_codes'],
            },
          }),
        }),
      ]),
    );
    render(<SessionsPage />);
    await screen.findByRole('link', { name: 'job-1' });
    const cpu = within(row('job-1')).getByText(/\u2265 80 CPU-s/);
    expect(cpu.getAttribute('title')).toBe(UNKNOWN_REASON_SENTENCE);
    expect(screen.queryByText(/deadbeef|uid=1001/)).not.toBeInTheDocument();
  });
});
