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
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AdminUsage,
  AdminUsageAggregate,
  AdminUsageBreakdown,
  AdminUsageFilters,
} from '@/lib/api/adminUsage';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
// A reactive stand-in for the router: setting ``value`` notifies subscribers,
// which is how a real URL change re-renders the mounted page.
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

const DAY_MS = 86_400_000;

function aggregate(overrides: Partial<AdminUsageAggregate> = {}): AdminUsageAggregate {
  return {
    run_count: 4,
    attempt_count: 4,
    totals: {
      calls: 30,
      tokens_in: 40_086,
      tokens_out: 413,
      cache_read: 19_712,
      cache_write: null,
      reasoning: null,
      spent_usd: '1.23456',
    },
    unknown_rows: {
      tokens_in: 0,
      tokens_out: 0,
      cache_read: 0,
      cache_write: 2,
      reasoning: 4,
      spent_usd: 0,
    },
    estimated_calls: 0,
    oldest_queried_at: '2026-09-09T10:01:00Z',
    runs_with_snapshot: 4,
    missing_attempts: 0,
    status_counts: { available: 4, partial: 0, stale: 0, pending: 0, unavailable: 0 },
    active_runs: 0,
    refreshing_runs: 0,
    ...overrides,
  };
}

function costing(spent: string, overrides: Partial<AdminUsageAggregate> = {}) {
  return aggregate({ totals: { ...aggregate().totals!, spent_usd: spent }, ...overrides });
}

function breakdown(
  groups: { key: string | null; usage?: AdminUsageAggregate }[],
  other: AdminUsageAggregate | null = null,
  groupCount?: number,
): AdminUsageBreakdown {
  return {
    // The API reports how many groups exist before the first twenty are taken,
    // so a remainder row means the total is larger than what is shown.
    group_count: groupCount ?? groups.length,
    groups: groups.map((group) => ({ key: group.key, usage: group.usage ?? aggregate() })),
    other: other ? { key: null, usage: other } : null,
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
        usage: aggregate(),
      },
      {
        date: '2026-09-04',
        since: '2026-09-04T00:00:00Z',
        until: '2026-09-04T09:00:00Z',
        usage: aggregate({ run_count: 0, attempt_count: 0, totals: null, runs_with_snapshot: 0 }),
      },
    ],
    by_user: breakdown([
      { key: 'owner-one', usage: costing('1', { run_count: 3 }) },
      { key: 'owner-two', usage: costing('0.23456', { run_count: 1 }) },
    ]),
    by_model: breakdown(
      [
        { key: 'model-a', usage: costing('1.2', { run_count: 2 }) },
        { key: null, usage: aggregate({ run_count: 1, totals: null, runs_with_snapshot: 0 }) },
      ],
      costing('0.03456', { run_count: 1 }),
      22,
    ),
    ...overrides,
  };
}

/**
 * The selections the page read, without the comparison reads: a comparison
 * ends exactly where the selection it belongs to starts.
 */
function reads(): AdminUsageFilters[] {
  const calls = vi.mocked(getAdminUsage).mock.calls.map(([filters]) => filters);
  return calls.filter((filters) => !calls.some((other) => other.since === filters.until));
}

/** Resolve a selection and its comparison with different numbers. */
function answer(current: AdminUsage, previous: AdminUsage | Error) {
  vi.mocked(getAdminUsage).mockImplementation(async (filters) => {
    const calls = vi.mocked(getAdminUsage).mock.calls.map(([f]) => f);
    const comparison = calls.some((other) => other.since === filters.until);
    if (!comparison) return current;
    if (previous instanceof Error) throw previous;
    return previous;
  });
}

/** Wait for a selection's numbers: the chart is drawn only once they arrived. */
async function loaded() {
  await screen.findByRole('group', { name: 'Chart' });
}

/** The value a headline shows for one label. */
function headline(label: string): HTMLElement {
  const term = screen.getAllByRole('term').find((element) => element.textContent === label);
  if (!term?.nextElementSibling) throw new Error(`no headline ${label}`);
  return term.nextElementSibling as HTMLElement;
}

function params(href: string | null): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(href!.slice(href!.indexOf('?') + 1)));
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  vi.mocked(getAdminUsage).mockResolvedValue(usage());
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

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

  it('defaults to an absolute seven-day window and compares it with the week before', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const [selection] = reads();
    expect(Date.parse(selection.until!) - Date.parse(selection.since!)).toBe(7 * DAY_MS);
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    const comparison = vi.mocked(getAdminUsage).mock.calls[1][0];
    expect(comparison.until).toBe(selection.since);
    expect(Date.parse(comparison.until!) - Date.parse(comparison.since!)).toBe(7 * DAY_MS);
    expect(screen.getByLabelText('Turn created')).toHaveValue('7');
    expect(
      within(screen.getByRole('navigation', { name: 'Admin sections' })).getByRole('link', {
        name: 'Usage',
      }),
    ).toHaveAttribute('aria-current', 'page');
  });

  it('shows the headline numbers, shortened, with the exact figures in tooltips', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    expect(headline('Cost')).toHaveTextContent('$1.23');
    expect(headline('Cost')).toHaveAttribute('title', '1.23456');
    expect(headline('Turns')).toHaveTextContent('4');
    expect(headline('Model calls')).toHaveTextContent('30');
    expect(headline('Input tokens')).toHaveTextContent('40.1K');
    expect(headline('Input tokens')).toHaveAttribute('title', '40,086');
    expect(headline('Output tokens')).toHaveTextContent('413');
    expect(screen.getByText('49% cached')).toBeInTheDocument();
  });

  it('states the change from the previous window', async () => {
    answer(usage(), usage({ summary: costing('1', { run_count: 2 }) }));
    render(<AdminUsagePageComponent />);
    const cost = await screen.findByText('+23%');
    expect(cost).toHaveAttribute('title', 'Previous 7 days: $1.00');
    expect(cost).toHaveTextContent('from the previous 7 days');
    expect(screen.getByText('+100%')).toBeInTheDocument();
  });

  it('shows the numbers without a change when the previous window cannot be read', async () => {
    answer(usage(), new APIError('SERVER_ERROR', 'boom', 500));
    render(<AdminUsagePageComponent />);
    await loaded();
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    expect(headline('Cost')).toHaveTextContent('$1.23');
    expect(screen.queryByText(/%$/, { selector: 'span[title]' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('renders unrecorded usage as unknown rather than as zero', async () => {
    vi.mocked(getAdminUsage).mockResolvedValue(
      usage({
        summary: aggregate({
          totals: null,
          unknown_rows: null,
          runs_with_snapshot: 0,
          status_counts: { available: 0, partial: 0, stale: 0, pending: 4, unavailable: 0 },
        }),
        days: [
          {
            date: '2026-09-03',
            since: '2026-09-03T00:00:00Z',
            until: '2026-09-04T00:00:00Z',
            usage: aggregate({ totals: null, unknown_rows: null, runs_with_snapshot: 0 }),
          },
        ],
      }),
    );
    render(<AdminUsagePageComponent />);
    await loaded();
    // The day is marked as unknown, and read out as such, not drawn as $0.
    expect(
      screen.getByRole('link', { name: 'Sep 3 · 4 turns, no usage recorded yet' }),
    ).toBeInTheDocument();
    expect(headline('Cost')).toHaveTextContent('—');
    expect(headline('Model calls')).toHaveTextContent('—');
    // Turns come from the database and are known either way.
    expect(headline('Turns')).toHaveTextContent('4');
    expect(screen.getByText(/4 turns not fully counted/)).toBeInTheDocument();
  });

  it('says so when no turn was created in the window', async () => {
    vi.mocked(getAdminUsage).mockResolvedValue(
      usage({
        summary: aggregate({
          run_count: 0,
          attempt_count: 0,
          runs_with_snapshot: 0,
          status_counts: { available: 0, partial: 0, stale: 0, pending: 0, unavailable: 0 },
          totals: null,
          unknown_rows: null,
        }),
        by_user: { group_count: 0, groups: [], other: null },
        by_model: { group_count: 0, groups: [], other: null },
        days: [],
      }),
    );
    render(<AdminUsagePageComponent />);
    await screen.findByText('No turns were created in this window.');
    expect(screen.queryByRole('term')).not.toBeInTheDocument();
  });

  it('says why the numbers are not final only when they are not', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    expect(screen.queryByText(/Not final/)).not.toBeInTheDocument();
    cleanup();

    vi.mocked(getAdminUsage).mockResolvedValue(
      usage({
        summary: aggregate({
          active_runs: 1,
          status_counts: { available: 2, partial: 1, stale: 0, pending: 1, unavailable: 0 },
          unknown_rows: { ...aggregate().unknown_rows!, spent_usd: 3 },
        }),
      }),
    );
    render(<AdminUsagePageComponent />);
    const line = await screen.findByText(/Not final/);
    expect(line).toHaveTextContent(
      'Not final: 1 turn still running · 2 turns not fully counted · 3 model calls without a recorded cost.',
    );
    expect(line.getAttribute('title')).toContain('1 turn without usage yet');
    expect(line.getAttribute('title')).toContain('1 turn partly counted');
  });

  it('links each day of the chart to that day in Activity', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const [selection] = reads();
    const day = screen.getByRole('link', { name: 'Sep 3 · $1.23 · 4 turns' });
    expect(params(day.getAttribute('href'))).toEqual({
      since: '2026-09-03T00:00:00Z',
      until: '2026-09-04T00:00:00Z',
    });
    expect(day.getAttribute('href')!.startsWith('/admin/activity?')).toBe(true);
    // A day the window cuts is named as part of one, and an empty day as such.
    expect(
      screen.getByRole('link', { name: 'Sep 4 (part of the day) · no turns' }),
    ).toBeInTheDocument();
    expect(selection.user_id).toBeUndefined();
  });

  it('switches the chart between metrics', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const chart = screen.getByRole('group', { name: 'Chart' });
    fireEvent.click(within(chart).getByRole('button', { name: 'Input tokens' }));
    expect(within(chart).getByRole('button', { name: 'Input tokens' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('link', { name: 'Sep 3 · 40.1K · 4 turns' })).toBeInTheDocument();
  });

  it('narrows the whole page to a user and keeps the window', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const [first] = reads();
    fireEvent.click(screen.getByRole('button', { name: 'Show only owner-one' }));
    await waitFor(() => expect(reads()).toHaveLength(2));
    expect(reads()[1]).toEqual({ since: first.since, until: first.until, user_id: 'owner-one' });
    // The comparison is narrowed the same way.
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(4));
    expect(vi.mocked(getAdminUsage).mock.calls[3][0].user_id).toBe('owner-one');

    fireEvent.click(await screen.findByRole('button', { name: 'Remove the user filter' }));
    await waitFor(() => expect(reads()).toHaveLength(3));
    expect(reads()[2]).toEqual({ since: first.since, until: first.until });
  });

  it('links a user, and the whole selection, to Activity', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const [selection] = reads();
    expect(
      params(screen.getByRole('link', { name: 'Open owner-one in Activity' }).getAttribute('href')),
    ).toEqual({ since: selection.since, until: selection.until, user_id: 'owner-one' });
    const whole = screen.getByRole('link', { name: /^Open in Activity/ });
    expect(whole.getAttribute('href')!.startsWith('/admin/activity?')).toBe(true);
    expect(params(whole.getAttribute('href'))).toEqual({
      since: selection.since,
      until: selection.until,
    });
  });

  it('offers no filter for an unknown model or for the remainder', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const models = screen.getByRole('region', { name: 'Models' });
    // Each says why it has no filter rather than leaving it unexplained.
    expect(within(models).getByText('Unknown model').closest('li')!.title).toContain(
      'An unknown model cannot be filtered by.',
    );
    expect(within(models).getByText('Other · 20 models').closest('li')!.title).toContain(
      'they are not one filter',
    );
    expect(within(models).getAllByRole('button')).toHaveLength(1);
    expect(within(models).getAllByRole('link')).toHaveLength(1);
    expect(within(models).getByRole('button', { name: 'Show only model-a' })).toBeInTheDocument();
  });

  it('re-anchors the window when another one is chosen, keeping the identities', async () => {
    search.value = 'since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&model=model-a';
    render(<AdminUsagePageComponent />);
    await loaded();
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await waitFor(() => expect(reads()).toHaveLength(2));
    const second = reads()[1];
    expect(Date.parse(second.until!) - Date.parse(second.since!)).toBe(30 * DAY_MS);
    expect(Date.now() - Date.parse(second.until!)).toBeLessThan(60_000);
    expect(second.model).toBe('model-a');
    expect(screen.getByLabelText('Turn created')).toHaveValue('30');
  });

  it('re-anchors a window chosen here on Refresh', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-28T09:00:00.000Z'));
    render(<AdminUsagePageComponent />);
    await loaded();
    vi.setSystemTime(new Date('2026-09-28T10:00:00.000Z'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(reads()).toHaveLength(2));
    expect(reads()[1]).toEqual({
      since: '2026-09-21T10:00:00.000Z',
      until: '2026-09-28T10:00:00.000Z',
    });
  });

  it('keeps a linked window exactly on Refresh', async () => {
    search.value = 'since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z';
    render(<AdminUsagePageComponent />);
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(reads()).toHaveLength(2));
    expect(reads()[1]).toEqual(reads()[0]);
  });

  it('keeps the previous numbers when a refresh of the same window fails', async () => {
    // A linked window is re-read exactly, so its numbers stay on screen. A
    // window chosen here moves to now, which is another selection.
    search.value = 'since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z';
    render(<AdminUsagePageComponent />);
    await loaded();
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    vi.mocked(getAdminUsage).mockRejectedValueOnce(new APIError('SERVER_ERROR', 'boom', 500));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The numbers below are from the previous read.');
    expect(headline('Cost')).toHaveTextContent('$1.23');
  });

  it('clears the numbers and shows the permission state when access is revoked', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    await waitFor(() => expect(getAdminUsage).toHaveBeenCalledTimes(2));
    vi.mocked(getAdminUsage).mockRejectedValue(new APIError('INVALID_TOKEN', 'no', 403));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('$1.23')).not.toBeInTheDocument();
  });

  it('discards a response for a selection that is no longer applied', async () => {
    const pending: { filters: AdminUsageFilters; resolve: (value: AdminUsage) => void }[] = [];
    vi.mocked(getAdminUsage).mockImplementation(
      (filters) => new Promise<AdminUsage>((resolve) => pending.push({ filters, resolve })),
    );
    render(<AdminUsagePageComponent />);
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await waitFor(() => expect(pending).toHaveLength(4));
    // The first selection's answer arrives last and must not be shown.
    pending[2].resolve(usage({ summary: aggregate({ run_count: 9 }) }));
    pending[0].resolve(usage({ summary: aggregate({ run_count: 5 }) }));
    await waitFor(() => expect(headline('Turns')).toHaveTextContent('9'));
    await act(async () => {});
    expect(headline('Turns')).toHaveTextContent('9');
  });

  it('never shows one selection’s numbers while another loads', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    const pending: ((value: AdminUsage) => void)[] = [];
    vi.mocked(getAdminUsage).mockImplementation(
      () => new Promise<AdminUsage>((resolve) => pending.push(resolve)),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Show only owner-one' }));
    // Links built from the old numbers would point at turns they do not describe.
    await screen.findByText('Loading usage…');
    expect(screen.queryByRole('link', { name: 'Open owner-one in Activity' })).toBeNull();
    expect(screen.queryByRole('term')).not.toBeInTheDocument();
    await waitFor(() => expect(pending).toHaveLength(2));
    pending[0](usage({ summary: aggregate({ run_count: 3 }) }));
    await waitFor(() => expect(headline('Turns')).toHaveTextContent('3'));
  });

  it('writes its selection into the address bar without reading it twice', async () => {
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    await loaded();
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const written = String(replace.mock.calls.at(-1)?.[2]);
    const [selection] = reads();
    expect(params(written)).toEqual({ since: selection.since, until: selection.until });
    // The router hands the write back as a new query. It is the selection
    // already on screen, so it is not read again.
    await act(async () => search.set(written.slice(written.indexOf('?') + 1)));
    expect(getAdminUsage).toHaveBeenCalledTimes(2);
    // Another address, such as the Usage tab itself, is a new selection.
    await act(async () => search.set(''));
    await waitFor(() => expect(reads()).toHaveLength(2));
    replace.mockRestore();
  });

  it('follows a URL that changes while the page stays mounted', async () => {
    render(<AdminUsagePageComponent />);
    await loaded();
    search.set('since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&user_id=owner-two');
    await waitFor(() => expect(reads()).toHaveLength(2));
    expect(reads()[1]).toEqual({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      user_id: 'owner-two',
    });
    expect(await screen.findByTitle('owner-two')).toBeInTheDocument();
    expect(screen.getByLabelText('Turn created')).toHaveValue('custom');
    expect(screen.getByText('Aug 1 – Aug 8')).toBeInTheDocument();
  });

  it('reports a link this page cannot serve instead of widening it', async () => {
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&host=x';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Unsupported filter: host');
    expect(getAdminUsage).not.toHaveBeenCalled();
    // Writing the default selection back would replace the link being
    // reported, and the remount that follows would read every owner instead.
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
    replace.mockRestore();
  });

  it('reports an unusable window without replacing it either', async () => {
    search.value = 'since=2026-09-08T00:00:00.000Z&until=2026-09-01T00:00:00.000Z';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    expect(await screen.findByRole('alert')).toHaveTextContent('empty or inverted window');
    await act(async () => {});
    expect(replace).not.toHaveBeenCalled();
    expect(getAdminUsage).not.toHaveBeenCalled();
    replace.mockRestore();
  });

  it('lets choosing a window replace a link this page cannot serve', async () => {
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&host=x';
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<AdminUsagePageComponent />);
    await screen.findByRole('alert');
    fireEvent.change(screen.getByLabelText('Turn created'), { target: { value: '30' } });
    await waitFor(() => expect(reads()).toHaveLength(1));
    const [selection] = reads();
    expect(Date.parse(selection.until!) - Date.parse(selection.since!)).toBe(30 * DAY_MS);
    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(String(replace.mock.calls.at(-1)?.[2])).not.toContain('host=x');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    replace.mockRestore();
  });
});
