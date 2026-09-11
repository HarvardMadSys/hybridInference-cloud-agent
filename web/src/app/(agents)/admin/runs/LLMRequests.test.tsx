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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function renderPanel(
  overrides: { jobId?: string; attemptId?: number; running?: boolean; denied?: () => void } = {},
) {
  return render(
    <LLMRequests
      jobId={overrides.jobId ?? 'job-1'}
      attemptId={overrides.attemptId ?? 10}
      running={overrides.running ?? false}
      onAccessDenied={overrides.denied ?? (() => {})}
    />,
  );
}

async function expand() {
  fireEvent.click(screen.getByText('LLM requests'));
  // Opening is what triggers the metadata read; nothing runs while collapsed.
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
    await expand();
    expect(await screen.findByTitle('0.00978569')).toHaveTextContent('$0.0097');
    // A real charge below the displayed places is marked, never shown as free.
    expect(screen.getByTitle('0.00000001')).toHaveTextContent('<$0.0001');
    expect(screen.getByTitle('0')).toHaveTextContent('$0');
    expect(screen.queryByTitle('Unavailable')).not.toBeInTheDocument();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThanOrEqual(1);
  });

  it('separates a null duration from a measured zero', () => {
    expect(durationLabel(null)).toBe('Not reported');
    expect(durationLabel(0)).toBe('0 ms');
    expect(statusLabel(null)).toBe('Unavailable');
    expect(statusLabel(429)).toBe('HTTP 429');
  });

  it('shows the served model only when it differs from the requested one', () => {
    expect(modelLabel(row()).text).toBe('model-alias → model-version');
    expect(modelLabel(row({ served_model_id: null })).text).toBe('model-alias');
    expect(modelLabel(row({ served_model_id: 'model-alias' })).text).toBe('model-alias');
    expect(modelLabel(row({ model: null, served_model_id: null })).text).toBe('Unavailable');
  });

  it('explains an unavailable source without promising a gateway read', () => {
    expect(sourceExplanation(source({ reason: 'binding_changed' }))).toMatch(/different gateway/);
    expect(sourceExplanation(source({ reason: null }))).toMatch(/unavailable/);
  });
});

describe('LLM requests panel', () => {
  it('stays collapsed and reads nothing until it is opened', () => {
    renderPanel();
    expect(sources).not.toHaveBeenCalled();
    expect(requests).not.toHaveBeenCalled();
  });

  it('reads the only queryable source and shows its window and read time', async () => {
    renderPanel();
    await expand();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    expect(sources).toHaveBeenCalledWith(
      'job-1',
      { attempt_id: 10, limit: 100 },
      expect.anything(),
    );
    expect(requests).toHaveBeenCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
    expect(await screen.findByText(/Execution 1 · Window/)).toBeInTheDocument();
    expect(screen.getByText(/Read at/)).toBeInTheDocument();
    expect(screen.getByText(/Logs may arrive later/)).toBeInTheDocument();
    const rowCells = screen.getByRole('row', {
      name: /model-alias → model-version HTTP 200 40,086 \/ 413/,
    });
    expect(within(rowCells).getByTitle('0.00978569')).toHaveTextContent('$0.0097');
    expect(within(rowCells).getByText('240 ms')).toBeInTheDocument();
    expect(within(rowCells).getByText('850 ms')).toBeInTheDocument();
  });

  it('asks which execution to read only when more than one can be read', async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source(),
        source({ source_id: '43', attempt_id: 11, attempt_no: 2, window_state: 'open' }),
      ]),
    );
    renderPanel();
    await expand();
    const selector = await screen.findByRole('combobox');
    expect(requests).not.toHaveBeenCalled();
    fireEvent.change(selector, { target: { value: '43' } });
    await waitFor(() =>
      expect(requests).toHaveBeenCalledWith(
        'job-1',
        { source_id: '43', limit: 50 },
        expect.anything(),
      ),
    );
  });

  it('keeps an unattributed execution visible instead of reading nothing', async () => {
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
    await expand();
    expect(await screen.findByText(/never attributed to a gateway ledger/)).toBeInTheDocument();
    expect(screen.getByText(/no queryable gateway source/)).toBeInTheDocument();
    expect(requests).not.toHaveBeenCalled();
  });

  it('says a run has not started rather than showing an empty ledger', async () => {
    sources.mockResolvedValue(sourcesPage([], 'not_started'));
    renderPanel();
    await expand();
    expect(await screen.findByText('No execution has started in this run.')).toBeInTheDocument();
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
    await expand();
    expect((await screen.findAllByText('Unavailable')).length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByText('Not reported').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Contains estimated usage')).toBeInTheDocument();
  });

  it('appends the next page and says how much of the window is loaded', async () => {
    requests
      .mockResolvedValueOnce(page([row()], 'cursor-1'))
      .mockResolvedValueOnce(
        page([row({ request_id: 'req_second', request_started_at: '2026-09-11T03:20:06Z' })]),
      );
    renderPanel();
    await expand();
    const more = await screen.findByRole('button', { name: 'Load more' });
    fireEvent.click(more);
    await waitFor(() => expect(screen.getByText(/Loaded 2 records/)).toBeInTheDocument());
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50, cursor: 'cursor-1' },
      expect.anything(),
    );
    expect(screen.getByText('req_second')).toBeInTheDocument();
  });

  it('keeps a failed page and its cursor for a retry instead of ending the list', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel();
    await expand();
    requests.mockRejectedValueOnce(new APIError('SERVER_ERROR', 'gateway down', 503));
    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText(/Server error/);
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument();
    expect(screen.getByText('req_example')).toBeInTheDocument();
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
    renderPanel();
    await expand();
    await screen.findByText('req_example');
    requests.mockRejectedValueOnce(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText(/Service temporarily unavailable/)).toBeInTheDocument();
    expect(screen.getByText('req_example')).toBeInTheDocument();
    expect(screen.getByText(/Read at/)).toBeInTheDocument();
  });

  it('drops old pages and reads the first page again on refresh', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel();
    await expand();
    await screen.findByRole('button', { name: 'Load more' });
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_second' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('req_second');
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_third' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('req_third');
    expect(screen.queryByText('req_second')).not.toBeInTheDocument();
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
  });

  it('stops a late response from filling the execution that replaced it', async () => {
    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    const view = renderPanel();
    await expand();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    requests.mockResolvedValue(page([row({ request_id: 'req_current' })]));
    view.rerender(
      <LLMRequests jobId="job-1" attemptId={11} running={false} onAccessDenied={() => {}} />,
    );
    late.resolve(page([row({ request_id: 'req_stale' })]));
    expect(await screen.findByText('req_current')).toBeInTheDocument();
    expect(screen.queryByText('req_stale')).not.toBeInTheDocument();
  });

  it('hands a revoked session to the page instead of showing an empty list', async () => {
    const denied = vi.fn();
    requests.mockRejectedValue(new APIError('UNKNOWN_ERROR', 'expired', 403));
    renderPanel({ denied });
    await expand();
    await waitFor(() => expect(denied).toHaveBeenCalled());
  });

  it('asks for a restart when the continuation no longer describes the query', async () => {
    requests.mockRejectedValue(new APIError('request_cursor_expired', 'That page expired.', 409));
    renderPanel();
    await expand();
    const restart = await screen.findByRole('button', {
      name: 'Start again from the first page',
    });
    requests.mockResolvedValue(page([row()]));
    fireEvent.click(restart);
    await screen.findByText('req_example');
    expect(requests).toHaveBeenLastCalledWith(
      'job-1',
      { source_id: '42', limit: 50 },
      expect.anything(),
    );
  });

  it('re-reads a visible running execution but leaves a finished one alone', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const denied = vi.fn();
    const view = renderPanel({ running: true, denied });
    await expand();
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(15_000);
    await waitFor(() => expect(requests).toHaveBeenCalledTimes(2));

    view.rerender(
      <LLMRequests jobId="job-1" attemptId={10} running={false} onAccessDenied={denied} />,
    );
    await vi.advanceTimersByTimeAsync(30_000);
    expect(requests).toHaveBeenCalledTimes(2);
  });

  it('does not append a page that belongs to the source it replaced', async () => {
    sources.mockResolvedValue(
      sourcesPage([
        source(),
        source({ source_id: '43', attempt_id: 11, attempt_no: 2, window_state: 'open' }),
      ]),
    );
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_a' })], 'cursor-a'));
    renderPanel();
    await expand();
    const selector = await screen.findByRole('combobox');
    fireEvent.change(selector, { target: { value: '42' } });
    await screen.findByText('req_a');

    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    requests.mockResolvedValueOnce(page([row({ request_id: 'req_b' })], 'cursor-b'));
    fireEvent.change(selector, { target: { value: '43' } });
    await screen.findByText('req_b');

    await act(async () => {
      late.resolve(page([row({ request_id: 'req_late_a' })], 'cursor-a'));
      await late.promise;
    });
    expect(screen.queryByText('req_late_a')).not.toBeInTheDocument();
    expect(screen.queryByText('req_a')).not.toBeInTheDocument();
    expect(screen.getByText(/Loaded 1 record/)).toBeInTheDocument();

    // The continuation is the page on screen, not the one that arrived late.
    requests.mockResolvedValue(page([]));
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(requests).toHaveBeenLastCalledWith(
        'job-1',
        { source_id: '43', limit: 50, cursor: 'cursor-b' },
        expect.anything(),
      ),
    );
  });

  it('leaves the page button usable when a refresh cancels a page in flight', async () => {
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel();
    await expand();
    await screen.findByRole('button', { name: 'Load more' });

    const late = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(late.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(await screen.findByRole('button', { name: 'Loading…' })).toBeDisabled();

    requests.mockResolvedValueOnce(page([row({ request_id: 'req_refreshed' })], 'cursor-2'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('req_refreshed');
    expect(await screen.findByRole('button', { name: 'Load more' })).toBeEnabled();

    await act(async () => {
      late.resolve(page([row({ request_id: 'req_late' })]));
      await late.promise;
    });
    expect(screen.queryByText('req_late')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Load more' })).toBeEnabled();
  });

  it('does not start a refresh while a page is already in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    requests.mockResolvedValueOnce(page([row()], 'cursor-1'));
    renderPanel({ running: true });
    await expand();
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

  it('keeps the window that produced the rows when a refresh fails', async () => {
    requests.mockResolvedValueOnce(page([row()]));
    renderPanel();
    await expand();
    await screen.findByText('req_example');
    expect(screen.getByText(/3:20:00 AM/)).toBeInTheDocument();
    expect(screen.getByText(/3:21:00 AM/)).toBeInTheDocument();

    // The stored reference now covers a longer window, but the read failed, so
    // the rows on screen still belong to the window they were read from.
    sources.mockResolvedValue(sourcesPage([source({ window_until: '2026-09-11T03:30:00Z' })]));
    requests.mockRejectedValueOnce(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/Service temporarily unavailable/);
    expect(screen.getByText(/3:21:00 AM/)).toBeInTheDocument();
    expect(screen.queryByText(/3:30:00 AM/)).not.toBeInTheDocument();
    expect(screen.getByText('req_example')).toBeInTheDocument();
  });

  it('does not page a read a refresh is already replacing', async () => {
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_first' })], 'cursor-1'));
    renderPanel();
    await expand();
    await screen.findByText('req_first');

    const refreshed = deferred<AdminLlmRequestPage>();
    requests.mockReturnValueOnce(refreshed.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
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
    expect(await screen.findByText('req_fresh')).toBeInTheDocument();
    expect(screen.queryByText('req_first')).not.toBeInTheDocument();
    expect(screen.getByText(/3:25:00 AM/)).toBeInTheDocument();
    expect(screen.queryByText(/3:21:00 AM/)).not.toBeInTheDocument();
  });

  it('drops a page that was in flight when the read it belonged to was replaced', async () => {
    requests.mockResolvedValueOnce(page([row({ request_id: 'req_first' })], 'cursor-1'));
    renderPanel();
    await expand();
    await screen.findByText('req_first');

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
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('req_fresh');

    await act(async () => {
      oldPage.resolve(
        page(
          [row({ request_id: 'req_late', request_started_at: '2026-09-11T03:20:30Z' })],
          'cursor-1',
        ),
      );
      await oldPage.promise;
    });
    expect(screen.queryByText('req_late')).not.toBeInTheDocument();
    expect(screen.getByText(/3:25:00 AM/)).toBeInTheDocument();

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
    await expand();
    await screen.findByText('req_first');

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
    expect(screen.queryByText('req_other_window')).not.toBeInTheDocument();
    expect(screen.getByText('req_first')).toBeInTheDocument();
  });

  it("does not query the previous execution's source after switching execution", async () => {
    const denied = vi.fn();
    const sourcesForAttempt = (attemptId: number | undefined) =>
      Promise.resolve(
        attemptId === 11
          ? sourcesPage([source({ source_id: '77', attempt_id: 11, attempt_no: 2 })])
          : sourcesPage([source({ source_id: '42', attempt_id: 10, attempt_no: 1 })]),
      );
    sources.mockImplementation((_jobId, filters) => sourcesForAttempt(filters?.attempt_id));
    const first = page([row({ request_id: 'req_10' })]);
    const second = {
      ...page([row({ request_id: 'req_11' })]),
      source_id: '77',
      attempt_id: 11,
      attempt_no: 2,
    };
    requests.mockResolvedValueOnce(first).mockResolvedValue(second);

    const view = renderPanel({ attemptId: 10, denied });
    await expand();
    await screen.findByText('req_10');
    requests.mockClear();

    view.rerender(
      <LLMRequests jobId="job-1" attemptId={11} running={false} onAccessDenied={denied} />,
    );
    await screen.findByText('req_11');
    expect(screen.queryByText('req_10')).not.toBeInTheDocument();
    expect(requests.mock.calls.map((call) => call[1])).toEqual([{ source_id: '77', limit: 50 }]);
  });

  it('keeps the previous execution off the screen when its replacement has no sources', async () => {
    const denied = vi.fn();
    const sourcesForAttempt = (attemptId: number | undefined) => {
      if (attemptId === 11) {
        return Promise.reject(new APIError('SERVICE_UNAVAILABLE', 'unavailable', 503));
      }
      return Promise.resolve(
        sourcesPage([source({ source_id: '42', attempt_id: 10, attempt_no: 1 })]),
      );
    };
    sources.mockImplementation((_jobId, filters) => sourcesForAttempt(filters?.attempt_id));
    requests.mockResolvedValue(page([row({ request_id: 'req_10' })]));
    const view = renderPanel({ attemptId: 10, denied });
    await expand();
    await screen.findByText('req_10');
    requests.mockClear();

    view.rerender(
      <LLMRequests jobId="job-1" attemptId={11} running={false} onAccessDenied={denied} />,
    );
    expect(await screen.findByText(/Service temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('req_10')).not.toBeInTheDocument();
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
