// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  AdminLlmRequest,
  AdminLlmRequestPage,
  AdminLlmRequestSource,
} from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

vi.mock('@/lib/api/adminRuns', () => ({
  getAdminLlmRequestSources: vi.fn(),
  getAdminLlmRequests: vi.fn(),
}));

import { getAdminLlmRequestSources, getAdminLlmRequests } from '@/lib/api/adminRuns';
import {
  cacheHitRate,
  timePerOutputToken,
  continues,
  durationLabel,
  LLMRequests,
  modelLabel,
  sourceExplanation,
  statusLabel,
} from './LLMRequests';

const sources = vi.mocked(getAdminLlmRequestSources);
const requests = vi.mocked(getAdminLlmRequests);

const WINDOW = {
  since: '2026-09-11T03:20:00Z',
  until: '2026-09-11T03:21:00Z',
  basis: 'request_started_at' as const,
};

function source(overrides: Partial<AdminLlmRequestSource> = {}): AdminLlmRequestSource {
  return {
    source_id: '42',
    attempt_id: 10,
    attempt_no: 1,
    window_since: WINDOW.since,
    window_until: WINDOW.until,
    window_state: 'closed',
    availability: 'queryable',
    reason: null,
    ...overrides,
  };
}

function sourcesPage(list: AdminLlmRequestSource[], reason: string | null = null) {
  return {
    job_id: 'job-1',
    observed_at: '2026-09-11T03:22:00Z',
    sources: list,
    next_cursor: null,
    reason,
  };
}

function row(overrides: Partial<AdminLlmRequest> = {}): AdminLlmRequest {
  return {
    request_id: 'req_example',
    request_started_at: '2026-09-11T03:20:04Z',
    logged_at: '2026-09-11T03:20:05Z',
    model: 'model-alias',
    served_model_id: 'model-version',
    tokens_in: 40086,
    tokens_out: 413,
    cache_read: 19712,
    cache_write: null,
    reasoning: null,
    spent_usd: '0.00978569',
    ttft_ms: 240,
    latency_ms: 850,
    status_code: 200,
    usage_estimated: false,
    ...overrides,
  };
}

function page(list: AdminLlmRequest[], next: string | null = null): AdminLlmRequestPage {
  return {
    job_id: 'job-1',
    source_id: '42',
    attempt_id: 10,
    attempt_no: 1,
    window: WINDOW,
    source: 'gateway_api_logs',
    fetched_at: '2026-09-11T03:22:01Z',
    gateway_as_of: '2026-09-11T03:22:00Z',
    pagination_consistency: 'live_keyset',
    finalized: false,
    requests: list,
    next_cursor: next,
  };
}

/** A page of the second run of a turn that was restarted. */
function secondRunPage(list: AdminLlmRequest[], next: string | null = null): AdminLlmRequestPage {
  return { ...page(list, next), source_id: '43', attempt_id: 11, attempt_no: 2 };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function renderPanel(
  overrides: { jobId?: string; running?: boolean; skip?: number[]; denied?: () => void } = {},
) {
  return render(
    <LLMRequests
      jobId={overrides.jobId ?? 'job-1'}
      running={overrides.running ?? false}
      skipAttemptIds={overrides.skip}
      onAccessDenied={overrides.denied ?? (() => {})}
    />,
  );
}

async function waitForRead() {
  await waitFor(() => expect(sources).toHaveBeenCalled());
}

beforeEach(() => {
  vi.clearAllMocks();
  sources.mockResolvedValue(sourcesPage([source()]));
  requests.mockResolvedValue(page([row()]));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('field labels', () => {
  it('shows a recorded amount with the shared display convention', async () => {
    requests.mockResolvedValue(
      page([
        row({ request_id: 'req_round', spent_usd: '0.00978569' }),
        row({
          request_id: 'req_tiny',
          request_started_at: '2026-09-11T03:20:05Z',
          spent_usd: '0.00000001',
        }),
        row({
          request_id: 'req_zero',
          request_started_at: '2026-09-11T03:20:06Z',
          spent_usd: '0',
        }),
        row({
          request_id: 'req_none',
          request_started_at: '2026-09-11T03:20:07Z',
          spent_usd: null,
        }),
      ]),
    );
    renderPanel();
    await waitForRead();
    expect(await screen.findByTitle('0.00978569')).toHaveTextContent('$0.0097');
    // A real charge below the displayed places is marked, never shown as free.
    expect(screen.getByTitle('0.00000001')).toHaveTextContent('<$0.0001');
    expect(screen.getByTitle('0')).toHaveTextContent('$0');
    expect(screen.queryByTitle('Unavailable')).not.toBeInTheDocument();
    expect(screen.getByTitle('req_none').closest('tr')?.lastElementChild).toHaveTextContent('—');
  });

  it('separates a null duration from a measured zero', () => {
    expect(durationLabel(null)).toBe('—');
    expect(durationLabel(4954)).toBe('4.954 s');
    expect(durationLabel(0)).toBe('0 ms');
    expect(statusLabel(null)).toBe('—');
    expect(statusLabel(429)).toBe('HTTP 429');
  });

  it('shows the served model only when it differs from the requested one', () => {
    expect(modelLabel(row()).text).toBe('model-alias → model-version');
    expect(modelLabel(row({ served_model_id: null })).text).toBe('model-alias');
    expect(modelLabel(row({ served_model_id: 'model-alias' })).text).toBe('model-alias');
    expect(modelLabel(row({ model: null, served_model_id: null })).text).toBe('—');
  });

  it('computes cache hit rate from inclusive input and preserves zero hits', () => {
    expect(cacheHitRate(row({ tokens_in: 100, cache_read: 75, cache_write: 25 }))).toBe(75);
    expect(cacheHitRate(row({ tokens_in: 100, cache_read: 0 }))).toBe(0);
    for (const override of [
      { tokens_in: null },
      { tokens_in: 0 },
      { tokens_in: -1 },
      { tokens_in: Infinity },
      { cache_read: null },
      { cache_read: -1 },
      { cache_read: NaN },
      { cache_read: 50000 },
    ])
      expect(cacheHitRate(row(override))).toBeNull();
  });

  it('estimates TPOT only when timing and output support the calculation', () => {
    expect(timePerOutputToken(row({ latency_ms: 1100, ttft_ms: 100, tokens_out: 101 }))).toBe(10);
    expect(timePerOutputToken(row({ latency_ms: 100, ttft_ms: 100 }))).toBe(0);
    for (const override of [
      { tokens_out: null },
      { tokens_out: 0 },
      { tokens_out: 1 },
      { tokens_out: Infinity },
      { ttft_ms: null },
      { ttft_ms: -1 },
      { ttft_ms: 851 },
      { latency_ms: null },
      { latency_ms: -1 },
      { latency_ms: NaN },
    ])
      expect(timePerOutputToken(row(override))).toBeNull();
  });

  it('explains an unavailable source without promising a gateway read', () => {
    expect(sourceExplanation(source({ reason: 'binding_changed' }))).toMatch(/different gateway/);
    expect(sourceExplanation(source({ reason: null }))).toMatch(/unavailable/);
  });
});

describe('LLM requests panel', () => {
  it('loads on mount without disclosure, refresh, or information controls', async () => {
    const { container } = renderPanel();
    await screen.findByTitle('req_example');
    expect(sources).toHaveBeenCalledTimes(1);
    expect(requests).toHaveBeenCalledTimes(1);
    expect(container.querySelector('details')).toBeNull();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Loaded|Read at|Window|gateway ledger|Logs may arrive/),
    ).not.toBeInTheDocument();
  });

  it('reads the only queryable source and shows all metrics in a single row', async () => {
    renderPanel();
    await waitForRead();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    expect(sources).toHaveBeenCalledWith('job-1', { limit: 100 }, expect.anything());
    expect(requests).toHaveBeenCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
    const rowCells = (await screen.findByTitle('req_example')).closest('tr')!;
    expect(screen.getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual([
      'Request',
      'HTTP',
      'Input',
      'Output',
      'Cache read',
      'Cache write',
      'Cache hit rate',
      'TTFT',
      'TPOT',
      'Duration',
      'Cost',
    ]);
    expect(within(rowCells).getAllByRole('cell')).toHaveLength(11);
    expect(within(rowCells).getByText('03:20:04')).toHaveAttribute(
      'datetime',
      '2026-09-11T03:20:04Z',
    );
    expect(within(rowCells).getByTitle('HTTP 200')).toHaveTextContent('200');
    expect(within(rowCells).getByText('40,086')).toBeInTheDocument();
    expect(within(rowCells).getByText('413')).toBeInTheDocument();
    expect(within(rowCells).getByText('19,712')).toBeInTheDocument();
    expect(within(rowCells).getByText('49.2%')).toBeInTheDocument();
    expect(within(rowCells).getByText('1.48 ms/token')).toBeInTheDocument();
    expect(screen.queryByText(/≈/)).not.toBeInTheDocument();
    expect(within(rowCells).getByTitle('0.00978569')).toHaveTextContent('$0.0097');
    expect(within(rowCells).getByText('240 ms')).toBeInTheDocument();
    expect(within(rowCells).getByText('850 ms')).toBeInTheDocument();
  });

  it('lists every run of a restarted turn in the order the runs happened', async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source({
          source_id: '43',
          attempt_id: 11,
          attempt_no: 2,
          window_since: '2026-09-11T03:21:00Z',
          window_until: null,
          window_state: 'open',
        }),
        source(),
      ]),
    );
    requests.mockImplementation(async (_jobId, filters) =>
      filters.source_id === '43'
        ? secondRunPage([row({ request_id: 'req_second_run' })])
        : page([row({ request_id: 'req_first_run' })]),
    );
    renderPanel();
    await screen.findByTitle('req_first_run');
    await screen.findByTitle('req_second_run');
    // Nothing to choose: both runs are part of the turn.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    const tables = screen.getAllByRole('table');
    expect(tables.map((table) => table.getAttribute('aria-label'))).toEqual([
      'LLM requests, Run 1 of 2 · from Sep 11, 2026, 3:20:00 AM UTC',
      'LLM requests, Run 2 of 2 · from Sep 11, 2026, 3:21:00 AM UTC',
    ]);
    expect(within(tables[0]).getByTitle('req_first_run')).toBeInTheDocument();
    expect(within(tables[1]).getByTitle('req_second_run')).toBeInTheDocument();
  });

  it('keeps an unattributed run visible instead of reading nothing', async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source({
          source_id: null,
          availability: 'unavailable',
          reason: 'attribution_unavailable',
          window_since: null,
          window_until: null,
          window_state: 'unknown',
        }),
      ]),
    );
    renderPanel();
    await waitForRead();
    expect(await screen.findByText(/never attributed to a gateway ledger/)).toBeInTheDocument();
    expect(screen.getByText('No gateway source can be read for this turn.')).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalled();
  });

  it('says a turn has not started rather than showing an empty ledger', async () => {
    sources.mockResolvedValue(sourcesPage([], 'not_started'));
    renderPanel();
    await waitForRead();
    expect(await screen.findByText('This turn has not started.')).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalled();
  });

  it('keeps null metrics null and marks estimated usage', async () => {
    requests.mockResolvedValue(
      page([
        row({
          ttft_ms: null,
          latency_ms: null,
          status_code: null,
          cache_write: null,
          reasoning: null,
          usage_estimated: true,
          spent_usd: null,
        }),
      ]),
    );
    renderPanel();
    await waitForRead();
    const cells = within((await screen.findByTitle('req_example')).closest('tr')!);
    expect(cells.getAllByText('—')).toHaveLength(6);
    expect(cells.getByTitle('Contains estimated usage')).toHaveTextContent('Estimated');
  });

  it('shows measured zero cache usage separately from missing metrics', async () => {
    requests.mockResolvedValue(page([row({ cache_read: 0, cache_write: 0, tokens_out: 1 })]));
    renderPanel();
    const cells = within((await screen.findByTitle('req_example')).closest('tr')!).getAllByRole(
      'cell',
    );
    expect(cells[4]).toHaveTextContent(/^0$/);
    expect(cells[5]).toHaveTextContent(/^0$/);
    expect(cells[6]).toHaveTextContent('0.0%');
    expect(cells[8]).toHaveTextContent('—');
  });

  it.each(['sources', 'requests'])(
    'can retry an initial %s failure without a permanent refresh control',
    async (failedRead) => {
      const api = failedRead === 'sources' ? sources : requests;
      api.mockRejectedValueOnce(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
      renderPanel();
      fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
      await screen.findByTitle('req_example');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.queryByRole('button')).not.toBeInTheDocument();
    },
  );

  it('appends the next page without adding a loaded count', async () => {
    requests
      .mockResolvedValueOnce(page([row()], 'cursor-1'))
      .mockResolvedValueOnce(
        page([row({ request_id: 'req_second', request_started_at: '2026-09-11T03:20:06Z' })]),
      );
    renderPanel();
    await waitForRead();
    const more = await screen.findByRole('button', { name: 'Load more' });
    fireEvent.click(more);
    await screen.findByTitle('req_second');
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(screen.queryByText(/Loaded/)).not.toBeInTheDocument();
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50, cursor: 'cursor-1' },
      expect.anything(),
    );
    expect(screen.getByTitle('req_second')).toBeInTheDocument();
  });

  it('keeps a failed page and its cursor for a retry instead of ending the list', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel();
    await waitForRead();
    requests.mockRejectedValueOnce(new APIError('SERVER_ERROR', 'gateway down', 503));
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText(/Server error/);
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument();
    expect(screen.getByTitle('req_example')).toBeInTheDocument();
    requests.mockResolvedValueOnce(page([]));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(requests).toHaveBeenLastCalledWith(
        'job-1',
        { source_id: '42', limit: 50, cursor: 'cursor-1' },
        expect.anything(),
      ),
    );
  });

  it('keeps the rows it has when a refresh fails, and says how old they are', async () => {
    const view = renderPanel();
    await waitForRead();
    await screen.findByTitle('req_example');
    requests.mockRejectedValueOnce(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    expect(await screen.findByText(/Service temporarily unavailable/)).toBeInTheDocument();
    expect(screen.getByTitle('req_example')).toBeInTheDocument();
    expect(
      screen.getByText(/last successful read from Sep 11, 2026, 3:22:01 AM UTC/),
    ).toBeInTheDocument();
  });

  it('drops old pages and reads the first page again on refresh', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    const view = renderPanel();
    await waitForRead();
    await screen.findByRole('button', { name: 'Load more' });
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_second' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTitle('req_second');
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_third' })]));
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    await screen.findByTitle('req_third');
    expect(screen.queryByTitle('req_second')).not.toBeInTheDocument();
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
  });

  it('stops a late response from filling the turn that replaced it', async () => {
    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    const view = renderPanel();
    await waitForRead();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    requests.mockResolvedValue(page([row({ request_id: 'req_current' })]));
    view.rerender(<LLMRequests jobId="job-2" running={false} onAccessDenied={() => {}} />);
    late.resolve(page([row({ request_id: 'req_stale' })]));
    expect(await screen.findByTitle('req_current')).toBeInTheDocument();
    expect(screen.queryByTitle('req_stale')).not.toBeInTheDocument();
  });

  it('hands a revoked session to the page instead of showing an empty list', async () => {
    const denied = vi.fn();
    requests.mockRejectedValue(new APIError('UNKNOWN_ERROR', 'expired', 403));
    renderPanel({ denied });
    await waitForRead();
    await waitFor(() => expect(denied).toHaveBeenCalled());
  });

  it('asks for a restart when the continuation no longer describes the query', async () => {
    requests.mockRejectedValue(new APIError('request_cursor_expired', 'That page expired.', 409));
    renderPanel();
    await waitForRead();
    const restart = await screen.findByRole('button', {
      name: 'Start again from the first page',
    });
    requests.mockResolvedValue(page([row()]));
    fireEvent.click(restart);
    await screen.findByTitle('req_example');
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
  });

  it('re-reads a visible running turn but leaves a finished one alone', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const denied = vi.fn();
    const view = renderPanel({ running: true, denied });
    await waitForRead();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(15_000);
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(2));

    view.rerender(<LLMRequests jobId="job-1" running={false} onAccessDenied={denied} />);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(requests).toHaveBeenCalledTimes(2);
  });

  it("keeps each run's pages in its own table", async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source(),
        source({ source_id: '43', attempt_id: 11, attempt_no: 2, window_state: 'open' }),
      ]),
    );
    requests.mockImplementation(async (_jobId, filters) => {
      if (filters.source_id === '43') return secondRunPage([row({ request_id: 'req_b' })]);
      return filters.cursor
        ? page([row({ request_id: 'req_a2' })])
        : page([row({ request_id: 'req_a' })], 'cursor-a');
    });
    renderPanel();
    await screen.findByTitle('req_a');
    await screen.findByTitle('req_b');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTitle('req_a2');
    const [first, latest] = screen.getAllByRole('table');
    expect(within(first).getByTitle('req_a2')).toBeInTheDocument();
    expect(within(latest).queryByTitle('req_a2')).not.toBeInTheDocument();
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50, cursor: 'cursor-a' },
      expect.anything(),
    );
  });

  it('leaves the page button usable when a refresh cancels a page in flight', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    const view = renderPanel();
    await waitForRead();
    await screen.findByRole('button', { name: 'Load more' });

    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByRole('button', { name: 'Loading…' })).toBeDisabled();

    requests.mockResolvedValueOnce(page([row({ request_id: 'req_refreshed' })], 'cursor-2'));
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    await screen.findByTitle('req_refreshed');
    expect(await screen.findByRole('button', { name: 'Load more' })).toBeEnabled();

    await act(async () => {
      late.resolve(page([row({ request_id: 'req_late' })]));
      await late.promise;
    });
    expect(screen.queryByTitle('req_late')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load more' })).toBeEnabled();
  });

  it('does not start a refresh while a page is already in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel({ running: true });
    await waitForRead();
    await screen.findByRole('button', { name: 'Load more' });

    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    const started = requests.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests.mock.calls.length).toBe(started);

    await act(async () => {
      late.resolve(page([]));
      await late.promise;
    });
  });

  it('keeps the previous cursor and window when a refresh fails', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    const view = renderPanel();
    await screen.findByTitle('req_example');
    sources.mockResolvedValue(sourcesPage([source({ window_until: '2026-09-11T03:30:00Z' })]));
    requests.mockRejectedValueOnce(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    await screen.findByText(/Service temporarily unavailable/);
    expect(screen.getByTitle('req_example')).toBeInTheDocument();
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_continued' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByTitle('req_continued');
    expect(screen.getByTitle('req_example')).toBeInTheDocument();
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50, cursor: 'cursor-1' },
      expect.anything(),
    );
  });

  it('does not page a read a refresh is already replacing', async () => {
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_first' })], 'cursor-1'));
    const view = renderPanel();
    await waitForRead();
    await screen.findByTitle('req_first');

    const refreshed = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(refreshed.promise);
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    expect(screen.getByRole('button', { name: 'Load more' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(requests).toHaveBeenCalledTimes(2);

    const fresh = {
      ...page(
        [row({ request_id: 'req_fresh', request_started_at: '2026-09-11T03:25:04Z' })],
        'cursor-2',
      ),
      window: {
        since: '2026-09-11T03:25:00Z',
        until: '2026-09-11T03:26:00Z',
        basis: 'request_started_at' as const,
      },
    };
    await act(async () => {
      refreshed.resolve(fresh);
      await refreshed.promise;
    });
    expect(await screen.findByTitle('req_fresh')).toBeInTheDocument();
    expect(screen.queryByTitle('req_first')).not.toBeInTheDocument();
    expect(screen.getByTitle('req_fresh')).toBeInTheDocument();
    expect(screen.getAllByRole('row')).toHaveLength(2);
  });

  it('drops a page that was in flight when the read it belonged to was replaced', async () => {
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_first' })], 'cursor-1'));
    const view = renderPanel();
    await waitForRead();
    await screen.findByTitle('req_first');

    const oldPage = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(oldPage.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    const fresh = {
      ...page([row({ request_id: 'req_fresh' })], 'cursor-2'),
      window: {
        since: '2026-09-11T03:25:00Z',
        until: '2026-09-11T03:26:00Z',
        basis: 'request_started_at' as const,
      },
    };
    requests.mockResolvedValueOnce(fresh);
    view.rerender(
      <LLMRequests jobId="job-1" running={false} refreshKey={1} onAccessDenied={() => {}} />,
    );
    await screen.findByTitle('req_fresh');

    await act(async () => {
      oldPage.resolve(
        page(
          [row({ request_id: 'req_late', request_started_at: '2026-09-11T03:20:30Z' })],
          'cursor-1',
        ),
      );
      await oldPage.promise;
    });
    expect(screen.queryByTitle('req_late')).not.toBeInTheDocument();
    expect(screen.getByTitle('req_fresh')).toBeInTheDocument();

    // The continuation on offer is still the fresh read's, not the stale one.
    requests.mockResolvedValue(page([], 'cursor-3'));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(requests).toHaveBeenLastCalledWith(
        'job-1',
        { source_id: '42', limit: 50, cursor: 'cursor-2' },
        expect.anything(),
      ),
    );
  });

  it('asks for a restart when a page answers a different read', async () => {
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_first' })], 'cursor-1'));
    renderPanel();
    await waitForRead();
    await screen.findByTitle('req_first');

    // A continuation whose window differs from the page on screen: the server
    // never sends one, so the panel must not merge it if it ever arrives.
    requests.mockResolvedValueOnce(
      page([row({ request_id: 'req_other_window' })], null) && {
        ...page([row({ request_id: 'req_other_window' })], null),
        window: {
          since: '2026-09-11T03:25:00Z',
          until: '2026-09-11T03:26:00Z',
          basis: 'request_started_at' as const,
        },
      },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByRole('button', { name: 'Start again from the first page' });
    expect(screen.queryByTitle('req_other_window')).not.toBeInTheDocument();
    expect(screen.getByTitle('req_first')).toBeInTheDocument();
  });

  it('lists nothing for a run that never started', async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source({
          source_id: null,
          attempt_id: 9,
          attempt_no: 1,
          availability: 'unavailable',
          reason: 'attribution_unavailable',
          window_since: null,
          window_until: null,
          window_state: 'unknown',
        }),
        source({ attempt_no: 2 }),
      ]),
    );
    renderPanel({ skip: [9] });
    await screen.findByTitle('req_example');
    // The run that could not start made no requests, so the turn reads as one run.
    expect(screen.queryByText(/never attributed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Run 1 of/)).not.toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveAttribute('aria-label', 'LLM requests');
  });

  it('keeps the previous turn off the screen when the next one has no sources', async () => {
    const denied = vi.fn();
    sources.mockImplementation((jobId) =>
      jobId === 'job-2'
        ? Promise.reject(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503))
        : Promise.resolve(sourcesPage([source()])),
    );
    requests.mockResolvedValue(page([row({ request_id: 'req_10' })]));
    const view = renderPanel({ denied });
    await waitForRead();
    await screen.findByTitle('req_10');
    requests.mockClear();

    view.rerender(<LLMRequests jobId="job-2" running={false} onAccessDenied={denied} />);
    expect(await screen.findByText(/Service temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByTitle('req_10')).not.toBeInTheDocument();
    expect(requests).not.toHaveBeenCalled();
  });

  it('treats only a page from the same source and window as a continuation', () => {
    const current = page([row()]);
    expect(continues({ ...current, requests: [] }, current)).toBe(true);
    expect(continues({ ...current, source_id: '43' }, current)).toBe(false);
    expect(
      continues(
        { ...current, window: { ...current.window, until: '2026-09-11T03:30:00Z' } },
        current,
      ),
    ).toBe(false);
  });
});
