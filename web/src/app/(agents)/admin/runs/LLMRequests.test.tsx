// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    await screen.findByText(/Execution 11/);
    expect(screen.queryByText('req_stale')).not.toBeInTheDocument();
    expect(screen.getByText('req_current')).toBeInTheDocument();
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
});
