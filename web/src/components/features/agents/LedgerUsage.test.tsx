// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobUsageApi,
  type AgentLedgerTotalsApi,
} from '@/lib/api/agents';

import { LedgerUsage } from './LedgerUsage';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({
  getAgentJobUsage: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
}));

const job = { id: 'ajob_1', state: 'done', runtime: 'claude-code' } as AgentJob;

const totals: AgentLedgerTotalsApi = {
  calls: 3,
  tokens_in: 1200,
  tokens_out: 80,
  cache_read: 1000,
  cache_write: 0,
  reasoning: null,
  spent_usd: '0.12345678',
};

function usage(): AgentJobUsageApi {
  const unknown = {
    tokens_in: 0,
    tokens_out: 0,
    cache_read: 0,
    cache_write: 0,
    reasoning: 3,
    spent_usd: 0,
  };
  return {
    ledger: {
      status: 'available',
      source: 'gateway_api_logs',
      attribution: 'grant_time_window',
      finalized: false,
      totals: { ...totals },
      unknown_rows: { ...unknown },
      estimated_calls: 0,
      queried_at: '2026-09-09T10:10:00Z',
      missing_attempts: 0,
      refreshing: false,
      attempts: [
        {
          attempt_id: 10,
          attempt_no: 1,
          status: 'available',
          reason: null,
          totals: { ...totals, calls: 1, tokens_in: 400 },
          unknown_rows: { ...unknown, reasoning: 1 },
          estimated_calls: 0,
          queried_at: '2026-09-09T10:10:00Z',
          windows: [
            {
              since: '2026-09-09T10:00:00Z',
              until: '2026-09-09T10:05:00Z',
              basis: 'request_started_at',
            },
          ],
        },
        {
          attempt_id: 11,
          attempt_no: 2,
          status: 'available',
          reason: null,
          totals: { ...totals, calls: 2, tokens_in: 800 },
          unknown_rows: { ...unknown, reasoning: 2 },
          estimated_calls: 0,
          queried_at: '2026-09-09T10:11:00Z',
          windows: [
            {
              since: '2026-09-09T10:05:00Z',
              until: '2026-09-09T10:09:00Z',
              basis: 'request_started_at',
            },
          ],
        },
      ],
    },
    runtime: {
      source: 'runtime',
      input_tokens: 200,
      output_tokens: 80,
      cache_read_tokens: 1000,
    },
  };
}

function gatewayRow(label: string): HTMLElement {
  return within(screen.getByRole('region', { name: 'Gateway ledger' })).getByText(label)
    .parentElement!;
}

beforeEach(() => {
  vi.mocked(getAgentJobUsage).mockReset().mockResolvedValue(usage());
  vi.mocked(refreshAgentJobUsage).mockReset().mockResolvedValue(usage());
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LedgerUsage', () => {
  it('keeps loading counts unknown and separates Gateway and runtime definitions', async () => {
    let finish!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    render(<LedgerUsage job={job} />);
    expect(screen.getByText('Loading usage…')).toBeInTheDocument();
    expect(gatewayRow('Model calls')).toHaveTextContent('—');
    await act(async () => finish(usage()));
    expect(gatewayRow('Input tokens')).toHaveTextContent('1,200');
    expect(gatewayRow('Spend (USD)')).toHaveTextContent('$0.12345678');
    expect(gatewayRow('Model calls')).toHaveTextContent('3');
    expect(gatewayRow('Reasoning tokens')).toHaveTextContent('—');
    expect(gatewayRow('Reasoning tokens')).toHaveTextContent('3 requests missing this value');
    // Unreported reasoning is named, not treated as a partial snapshot.
    expect(screen.getByText('Available snapshot')).toBeInTheDocument();
    expect(screen.getByText(/Core usage is complete/)).toHaveTextContent(
      'did not report reasoning tokens for 3 calls',
    );
    expect(screen.queryByText(/Partial totals/)).toBeNull();
    expect(screen.getByText(/Claude Code reports uncached input separately/)).toBeInTheDocument();
    const runtime = screen.getByRole('region', { name: 'Runtime reported usage' });
    expect(within(runtime).getByText('Reported input tokens').parentElement).toHaveTextContent(
      '200',
    );
    expect(screen.getByText(/Late logs may change these totals/)).toBeInTheDocument();
    expect(screen.getByText(/Oldest snapshot fetched/)).toHaveTextContent('10:10:00 UTC');
  });

  it('labels partial sums, missing attempts, null fields, and estimates without inventing zeros', async () => {
    const data = usage();
    data.ledger.missing_attempts = 1;
    data.ledger.estimated_calls = 1;
    data.ledger.totals!.spent_usd = null;
    data.ledger.unknown_rows!.spent_usd = 3;
    data.ledger.unknown_rows!.tokens_in = 2;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    render(<LedgerUsage job={job} />);
    await screen.findByText(/1 attempt without a usable snapshot/);
    expect(gatewayRow('Input tokens')).toHaveTextContent('1,200');
    expect(gatewayRow('Input tokens')).toHaveTextContent('2 requests missing this value');
    expect(gatewayRow('Spend (USD)')).toHaveTextContent('—');
    expect(screen.getByText('1 call includes estimated usage.')).toBeInTheDocument();
    expect(screen.getByText(/Partial totals: only known values are included/)).toBeInTheDocument();
    expect(screen.queryByText(/Core usage is complete/)).toBeNull();
  });

  it('selects cached attempts without extra requests and keeps runtime scope on all retries', async () => {
    render(<LedgerUsage job={job} />);
    const selector = await screen.findByRole('combobox', { name: 'Gateway scope' });
    expect(selector).toHaveValue('all');
    fireEvent.change(selector, { target: { value: '10' } });
    expect(screen.getByText('Gateway ledger · attempt 1')).toBeInTheDocument();
    expect(gatewayRow('Model calls')).toHaveTextContent('1');
    expect(gatewayRow('Input tokens')).toHaveTextContent('400');
    expect(screen.getByRole('list', { name: 'Request start windows' })).toHaveTextContent(
      '10:05:00 UTC',
    );
    expect(screen.getByText('Runtime reported · all attempts')).toBeInTheDocument();
    expect(screen.getByText('Reported input tokens').parentElement).toHaveTextContent('200');
    fireEvent.change(selector, { target: { value: 'all' } });
    expect(gatewayRow('Model calls')).toHaveTextContent('3');
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('renders legacy 404 as unavailable even when a running job has an old model-call value', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(<LedgerUsage job={{ ...job, state: 'running', usage: { turns: 0 } } as AgentJob} />);
    await screen.findByText('Ledger unavailable on this deployment.');
    expect(gatewayRow('Model calls')).toHaveTextContent('—');
    expect(gatewayRow('Spend (USD)')).toHaveTextContent('—');
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it('preserves a real zero call count for an empty measured window', async () => {
    const data = usage();
    data.ledger.totals!.calls = 0;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    render(<LedgerUsage job={job} />);
    await waitFor(() => expect(gatewayRow('Model calls')).toHaveTextContent('0'));
  });

  it('keeps cached data when refresh fails and never renders arbitrary error text', async () => {
    vi.mocked(refreshAgentJobUsage).mockRejectedValue(new Error('secret upstream details'));
    render(<LedgerUsage job={job} />);
    await screen.findByText('$0.12345678');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' }));
    await screen.findByText(/Usage could not be refreshed. Showing the last cached snapshot/);
    expect(gatewayRow('Spend (USD)')).toHaveTextContent('$0.12345678');
    expect(screen.getByText('Cached snapshot')).toBeInTheDocument();
    expect(screen.queryByText('secret upstream details')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh usage' })).toBeEnabled();
  });

  it('displays stale backend snapshots and translates only known reason codes', async () => {
    const data = usage();
    data.ledger.status = 'stale';
    data.ledger.attempts[0].status = 'stale';
    data.ledger.attempts[0].reason = 'private upstream body';
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    render(<LedgerUsage job={job} />);
    await screen.findByText('The latest refresh failed. Showing the last cached snapshot.');
    fireEvent.change(screen.getByRole('combobox', { name: 'Gateway scope' }), {
      target: { value: '10' },
    });
    expect(screen.getByText('Usage is unavailable for this attempt.')).toBeInTheDocument();
    expect(screen.queryByText('private upstream body')).not.toBeInTheDocument();
    expect(gatewayRow('Model calls')).toHaveTextContent('1');
  });

  it('polls a queued manual refresh while retaining the previous snapshot', async () => {
    vi.useFakeTimers();
    const queued = usage();
    queued.ledger.refreshing = true;
    const updated = usage();
    updated.ledger.totals!.calls = 4;
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(queued);
    vi.mocked(getAgentJobUsage).mockResolvedValueOnce(usage()).mockResolvedValue(updated);
    await act(async () => render(<LedgerUsage job={job} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' })));
    expect(screen.getByRole('button', { name: 'Refresh usage' })).toBeDisabled();
    expect(gatewayRow('Model calls')).toHaveTextContent('3');
    expect(refreshAgentJobUsage).toHaveBeenCalledWith('ajob_1', expect.any(AbortSignal));
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(gatewayRow('Model calls')).toHaveTextContent('4');
    expect(screen.getByRole('button', { name: 'Refresh usage' })).toBeEnabled();
  });

  it('queues one on-demand refresh when opening an active tracked run', async () => {
    vi.useFakeTimers();
    await act(async () => render(<LedgerUsage job={{ ...job, state: 'running' }} />));
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('checks again after a refresh queues throttled work without a busy flag', async () => {
    vi.useFakeTimers();
    const updated = usage();
    updated.ledger.totals!.calls = 4;
    vi.mocked(getAgentJobUsage).mockResolvedValueOnce(usage()).mockResolvedValue(updated);
    await act(async () => render(<LedgerUsage job={job} />));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh usage' })));
    expect(gatewayRow('Model calls')).toHaveTextContent('3');
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(gatewayRow('Model calls')).toHaveTextContent('4');
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });

  it('observes scheduled late-log refreshes without showing them as in-flight work', async () => {
    vi.useFakeTimers();
    const scheduled = usage();
    scheduled.ledger.next_refresh_at = new Date(Date.now() + 30_000).toISOString();
    const updated = usage();
    updated.ledger.totals!.calls = 4;
    updated.ledger.next_refresh_at = null;
    vi.mocked(getAgentJobUsage)
      .mockResolvedValueOnce(scheduled)
      .mockResolvedValueOnce(scheduled)
      .mockResolvedValue(updated);
    await act(async () => render(<LedgerUsage job={job} />));
    expect(screen.getByRole('button', { name: 'Refresh usage' })).toBeEnabled();
    expect(screen.queryByText('Refreshing…')).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(gatewayRow('Model calls')).toHaveTextContent('4');
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it('bounds automatic polling and cancels it when the drawer unmounts', async () => {
    vi.useFakeTimers();
    const pending = usage();
    pending.ledger.status = 'pending';
    pending.ledger.totals = null;
    pending.ledger.refreshing = true;
    vi.mocked(getAgentJobUsage).mockResolvedValue(pending);
    let unmount!: () => void;
    await act(async () => ({ unmount } = render(<LedgerUsage job={job} />)));
    await act(async () => vi.advanceTimersByTimeAsync(300_000));
    expect(getAgentJobUsage).toHaveBeenCalledTimes(21);
    expect(
      screen.getByText('Automatic updates paused. Refresh to check again.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh usage' })).toBeEnabled();
    unmount();
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAgentJobUsage).toHaveBeenCalledTimes(21);
  });

  it('aborts old reads and never shows another job’s late snapshot', async () => {
    let finishOld!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage)
      .mockImplementationOnce(() => new Promise((resolve) => (finishOld = resolve)))
      .mockResolvedValue(null);
    const { rerender } = render(<LedgerUsage job={job} />);
    const oldSignal = vi.mocked(getAgentJobUsage).mock.calls[0][1]!;
    rerender(<LedgerUsage job={{ ...job, id: 'ajob_2' }} />);
    expect(oldSignal.aborted).toBe(true);
    await screen.findByText('Ledger unavailable on this deployment.');
    await act(async () => finishOld(usage()));
    expect(screen.queryByText('$0.12345678')).not.toBeInTheDocument();
    expect(gatewayRow('Model calls')).toHaveTextContent('—');
  });
});
