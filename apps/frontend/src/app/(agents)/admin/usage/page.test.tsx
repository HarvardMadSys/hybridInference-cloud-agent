// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminUsage, AdminUsageAggregate, AdminUsageBreakdown } from '@/lib/api/adminUsage';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
// A reactive stand-in for the router: setting ``value`` notifies subscribers,
// which is how a real URL change re-renders the mounted panel.
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
  useSearchParams: () => new URLSearchParams(useSyncExternalStore(search.subscribe, search.read)),
}));
vi.mock('@/lib/api/adminUsage', () => ({ getAdminUsage: vi.fn() }));

import { getAdminUsage } from '@/lib/api/adminUsage';
import AdminUsagePageComponent from './page';

function aggregate(overrides: Partial<AdminUsageAggregate> = {}): AdminUsageAggregate {
  return {
    run_count: 4,
    attempt_count: 5,
    totals: {
      calls: 30,
      tokens_in: 40086,
      tokens_out: 413,
      cache_read: 19712,
      cache_write: null,
      reasoning: null,
      spent_usd: '0.00978569',
    },
    unknown_rows: {
      tokens_in: 0,
      tokens_out: 0,
      cache_read: 0,
      cache_write: 2,
      reasoning: 4,
      spent_usd: 0,
    },
    estimated_calls: 1,
    oldest_queried_at: '2026-09-09T10:01:00Z',
    runs_with_snapshot: 3,
    missing_attempts: 1,
    status_counts: {
      available: 2,
      partial: 1,
      stale: 0,
      pending: 0,
      unavailable: 1,
    },
    active_runs: 1,
    refreshing_runs: 1,
    ...overrides,
  };
}

function breakdown(
  groups: { key: string | null; usage?: Partial<AdminUsageAggregate> }[],
  other: Partial<AdminUsageAggregate> | null = null,
  groupCount?: number,
): AdminUsageBreakdown {
  return {
    // The API reports how many groups exist before the first twenty are taken,
    // so a remainder row means the total is larger than what is shown.
    group_count: groupCount ?? groups.length,
    groups: groups.map((group) => ({ key: group.key, usage: aggregate(group.usage) })),
    other: other ? { key: null, usage: aggregate(other) } : null,
  };
}

function usage(overrides: Partial<AdminUsage> = {}): AdminUsage {
  return {
    schema_version: 1,
    source: 'gateway_api_logs',
    attribution: 'grant_time_window',
    finalized: false,
    selection: {
      since: '2026-09-03T00:00:00Z',
      until: '2026-09-10T00:00:00Z',
      time_basis: 'job_created_at',
      day_timezone: 'UTC',
      model_basis: 'job_configured_model',
      user_id: null,
      model: null,
      runtime: null,
      repo: null,
    },
    generated_at: '2026-09-10T00:00:05Z',
    summary: aggregate(),
    days: [
      {
        date: '2026-09-03',
        since: '2026-09-03T00:00:00Z',
        until: '2026-09-04T00:00:00Z',
        usage: aggregate({
          run_count: 4,
          totals: { ...aggregate().totals!, spent_usd: '0.00978569' },
        }),
      },
    ],
    by_user: breakdown([
      { key: 'owner-one', usage: { run_count: 3 } },
      { key: 'owner-two', usage: { run_count: 1 } },
    ]),
    by_model: breakdown(
      [
        { key: 'model-a', usage: { run_count: 2 } },
        { key: null, usage: { run_count: 1, totals: null, runs_with_snapshot: 0 } },
      ],
      { run_count: 1 },
      22,
    ),
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  vi.mocked(getAdminUsage).mockResolvedValue(usage());
});
afterEach(cleanup);

describe('admin usage page', () => {
  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not read aggregated usage without a current admin permission: %j', (access) => {
    auth.access = access;
    render(<AdminUsagePageComponent />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getAdminUsage).not.toHaveBeenCalled();
  });

  it('defaults to an absolute seven-day window and states the basis', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    const filters = vi.mocked(getAdminUsage).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
    expect(screen.getByText(/Usage recorded for runs created in this period/)).toBeInTheDocument();
    expect(screen.getByText(/Dates use UTC/)).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Admin sections' })).toBeInTheDocument();
    expect(
      within(screen.getByRole('navigation', { name: 'Admin sections' })).getByRole('link', {
        name: 'Usage',
      }),
    ).toHaveAttribute('aria-current', 'page');
  });

  it('shows the four headline numbers with their small-amount and unknown detail', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    // The headline card is the first of the two: the day table repeats it.
    expect(screen.getAllByText('$0.0097')[0]).toHaveAttribute('title', '0.00978569');
    expect(screen.getAllByText('30').length).toBeGreaterThan(0);
    expect(screen.getAllByText('40,086').length).toBeGreaterThan(0);
    expect(screen.getAllByText('413').length).toBeGreaterThan(0);
    expect(
      screen.getByText('1 estimated · 6 recordings with an unreported value'),
    ).toBeInTheDocument();
  });

  it('reports coverage without calling it a completeness rate', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findByText('3 of 4 runs have a usage snapshot');
    expect(screen.getByText(/not a completeness rate/)).toBeInTheDocument();
    expect(screen.getByText(/1 executions without a readable snapshot/)).toBeInTheDocument();
    expect(screen.getByText(/1 partial snapshot/)).toBeInTheDocument();
    expect(screen.getByText(/Not finalized/)).toBeInTheDocument();
    expect(screen.getByText(/active runs may not be counted yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show metric detail' }));
    expect(screen.getByText(/recordings include an estimated usage value/)).toBeInTheDocument();
    expect(screen.getByText(/cache_write: 2 recordings unknown/)).toBeInTheDocument();
  });

  it('renders a missing snapshot as unavailable rather than as zero', async () => {
    vi.mocked(getAdminUsage).mockResolvedValue(
      usage({
        summary: aggregate({ totals: null, unknown_rows: null, runs_with_snapshot: 0 }),
      }),
    );
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText('0 of 4 runs have a usage snapshot')).toBeInTheDocument();
  });

  it('says no runs instead of a hundred percent when nothing is selected', async () => {
    vi.mocked(getAdminUsage).mockResolvedValue(
      usage({
        summary: aggregate({
          run_count: 0,
          attempt_count: 0,
          runs_with_snapshot: 0,
          status_counts: { available: 0, partial: 0, stale: 0, pending: 0, unavailable: 0 },
          totals: null,
          unknown_rows: null,
          estimated_calls: 0,
          active_runs: 0,
          refreshing_runs: 0,
        }),
        by_user: { group_count: 0, groups: [], other: null },
        by_model: { group_count: 0, groups: [], other: null },
        days: [],
      }),
    );
    render(<AdminUsagePageComponent />);
    await screen.findByText('No runs in this period');
    expect(screen.queryByText(/of 0 runs have a usage snapshot/)).not.toBeInTheDocument();
  });

  it('links a user row to the same cohort on the runs list', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    const row = screen.getByText('owner-one').closest('tr')!;
    const href = within(row).getByRole('link', { name: 'View sessions' }).getAttribute('href')!;
    const params = new URLSearchParams(href.slice(href.indexOf('?') + 1));
    const filters = vi.mocked(getAdminUsage).mock.calls[0][0];
    expect(href.startsWith('/admin/activity?')).toBe(true);
    expect(params.get('since')).toBe(filters.since);
    expect(params.get('until')).toBe(filters.until);
    expect(params.get('user_id')).toBe('owner-one');
  });

  it('links a created-date row to that day inside the applied window', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    fireEvent.click(screen.getByRole('button', { name: 'Created date' }));
    const row = screen.getByText('Sep 3, 2026').closest('tr')!;
    const href = within(row).getByRole('link', { name: 'View sessions' }).getAttribute('href')!;
    const params = new URLSearchParams(href.slice(href.indexOf('?') + 1));
    // The day replaces the window with that day's intersection, and the other
    // applied filters still travel with it.
    expect(params.get('since')).toBe(usage().days[0].since);
    expect(params.get('until')).toBe(usage().days[0].until);
  });

  it('does not offer a filter for an unknown model or for the remainder row', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    fireEvent.click(screen.getByRole('button', { name: 'Configured models' }));
    const unknown = screen.getByText('Unknown configuration').closest('tr')!;
    expect(within(unknown).queryByRole('link', { name: 'View sessions' })).not.toBeInTheDocument();
    expect(within(unknown).getByText('Cannot be filtered exactly')).toBeInTheDocument();
    // The remainder row is not a model anyone can filter by, so it carries no
    // link either; its label names how many groups it folds together.
    const rows = Array.from(document.querySelectorAll<HTMLElement>('tbody tr'));
    const remainder = rows.at(-1)!;
    expect(remainder.textContent).toContain('Other');
    expect(remainder.textContent).toContain('20 groups');
    expect(
      within(remainder).queryByRole('link', { name: 'View sessions' }),
    ).not.toBeInTheDocument();
  });

  it('keeps a link-supplied window when a non-time filter is applied', async () => {
    search.value = 'since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z';
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'owner-two' } });
    fireEvent.submit(screen.getByRole('form', { name: 'Usage filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    // Applying a non-time filter keeps the absolute window it arrived with,
    // including one that no preset produces.
    expect(vi.mocked(getAdminUsage).mock.calls[1][0]).toMatchObject({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      user_id: 'owner-two',
    });
    expect(screen.getByLabelText('Created')).toHaveValue('custom');
    expect(screen.queryByLabelText('User ID')).toHaveValue('owner-two');
  });

  it('re-anchors the window only when a preset is chosen again', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    fireEvent.change(screen.getByLabelText('Created'), { target: { value: '30' } });
    // Submitting the form applies exactly what the admin sees selected.
    fireEvent.submit(screen.getByRole('form', { name: 'Usage filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    const second = vi.mocked(getAdminUsage).mock.calls[1][0];
    expect(Date.parse(second.until!) - Date.parse(second.since!)).toBe(30 * 86_400_000);
    expect(screen.getByLabelText('Created')).toHaveValue('30');
  });

  it('keeps the window and filters on Refresh', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    const first = vi.mocked(getAdminUsage).mock.calls[0][0];
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminUsage).mock.calls[1][0]).toEqual(first);
  });

  it('shows the filter error and keeps the previous numbers on a failed refresh', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    vi.mocked(getAdminUsage).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByRole('alert');
    expect(screen.getAllByText('$0.0097').length).toBeGreaterThan(0);
    expect(screen.getByText(/last refresh failed/)).toBeInTheDocument();
  });

  it('clears the numbers and shows the permission state when access is revoked', async () => {
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    vi.mocked(getAdminUsage).mockRejectedValueOnce(new APIError('INVALID_TOKEN', 'no', 403));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Admin access required');
    expect(screen.queryAllByText('$0.0097')).toHaveLength(0);
  });

  it('discards an older response when the filters change before it resolves', async () => {
    const deferred: ((value: AdminUsage) => void)[] = [];
    vi.mocked(getAdminUsage).mockImplementation(
      () => new Promise<AdminUsage>((resolve) => deferred.push(resolve)),
    );
    render(<AdminUsagePageComponent />);
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'owner-two' } });
    await waitFor(() => expect(screen.getByLabelText('User ID')).toHaveValue('owner-two'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(deferred).toHaveLength(2));
    // The first response belongs to a filter set that is no longer applied.
    deferred[0](usage({ summary: aggregate({ run_count: 4, runs_with_snapshot: 3 }) }));
    deferred[1](usage({ summary: aggregate({ run_count: 9, runs_with_snapshot: 9 }) }));
    await screen.findByText('9 of 9 runs have a usage snapshot');
    expect(screen.queryByText('3 of 4 runs have a usage snapshot')).not.toBeInTheDocument();
  });

  it('clears the previous cohort while a new selection loads', async () => {
    // Numbers from one selection must never sit beside links built from
    // another: the table would point at runs the figures do not describe.
    let resolveSecond: (value: AdminUsage) => void = () => {};
    vi.mocked(getAdminUsage).mockResolvedValueOnce(
      usage({ summary: aggregate({ run_count: 4, runs_with_snapshot: 3 }) }),
    );
    render(<AdminUsagePageComponent />);
    await screen.findByText('3 of 4 runs have a usage snapshot');

    vi.mocked(getAdminUsage).mockImplementationOnce(
      () => new Promise<AdminUsage>((resolve) => (resolveSecond = resolve)),
    );
    fireEvent.change(screen.getByLabelText('Configured model'), { target: { value: 'model-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));

    expect(screen.queryByText('3 of 4 runs have a usage snapshot')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'View sessions' })).not.toBeInTheDocument();
    resolveSecond(usage({ summary: aggregate({ run_count: 2, runs_with_snapshot: 2 }) }));
    await screen.findByText('2 of 2 runs have a usage snapshot');
  });

  it('removes a cleared identity filter from the next request', async () => {
    vi.mocked(getAdminUsage).mockResolvedValue(usage());
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'owner-two' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminUsage).mock.calls[1][0].user_id).toBe('owner-two');

    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(3));
    expect(vi.mocked(getAdminUsage).mock.calls[2][0].user_id).toBeUndefined();
    expect(screen.getByLabelText('User ID')).toHaveValue('');
  });

  it('follows a URL that changes while the page stays mounted', async () => {
    // A history navigation to another selection re-reads it rather than
    // leaving the first query's numbers and form on screen.
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    expect(vi.mocked(getAdminUsage).mock.calls[0][0].user_id).toBeUndefined();

    search.set('since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&user_id=owner-two');
    await act(async () => {});
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminUsage).mock.calls[1][0]).toMatchObject({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      user_id: 'owner-two',
    });
    expect(screen.getByLabelText('User ID')).toHaveValue('owner-two');
  });

  it('reports a link this page cannot serve instead of widening it', async () => {
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&host=x';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    await screen.findByRole('alert');
    expect(screen.getByText(/Unsupported filter: host/)).toBeInTheDocument();
    expect(getAdminUsage).not.toHaveBeenCalled();
    // Writing the default selection back would replace the link being
    // reported, and the remount that follows would clear the error and read
    // every owner instead.
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    replace.mockRestore();
  });

  it('reports an unusable window without replacing it either', async () => {
    search.value = 'since=2026-09-08T00:00:00.000Z&until=2026-09-01T00:00:00.000Z';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    await screen.findByRole('alert');
    expect(screen.getByText(/empty or inverted window/)).toBeInTheDocument();
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    expect(getAdminUsage).not.toHaveBeenCalled();
    replace.mockRestore();
  });

  it('lets an explicit Apply replace a link this page cannot serve', async () => {
    // The error is a state, not a constant: choosing a window and applying it
    // is what the broken link could not provide, so it has to recover.
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&host=x';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    await screen.findByRole('alert');
    expect(getAdminUsage).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Created'), { target: { value: '30' } });
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'owner-two' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));

    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(1));
    const filters = vi.mocked(getAdminUsage).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(30 * 86_400_000);
    expect(filters.user_id).toBe('owner-two');
    // The applied selection is the one written to the address bar, without the
    // parameter that made the link unusable.
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(String(replace.mock.calls.at(-1)?.[2])).toContain('user_id=owner-two');
    expect(String(replace.mock.calls.at(-1)?.[2])).not.toContain('host=x');
    replace.mockRestore();
  });

  it('recovers from an unusable window the same way', async () => {
    search.value = 'since=2026-09-08T00:00:00.000Z&until=2026-09-01T00:00:00.000Z';
    render(<AdminUsagePageComponent />);
    await screen.findByRole('alert');
    expect(getAdminUsage).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(1));
    const filters = vi.mocked(getAdminUsage).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reads its window from the URL so a shared page keeps it', async () => {
    search.value = 'since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&model=model-a';
    render(<AdminUsagePageComponent />);
    await screen.findAllByText('Recorded cost');
    expect(vi.mocked(getAdminUsage).mock.calls[0][0]).toMatchObject({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      model: 'model-a',
    });
    expect(screen.getByLabelText('Configured model')).toHaveValue('model-a');
    // A week ending a month ago is not the last seven days: it reads Custom so
    // that changing another filter cannot silently re-anchor it to today.
    expect(screen.getByLabelText('Created')).toHaveValue('custom');
  });
});
