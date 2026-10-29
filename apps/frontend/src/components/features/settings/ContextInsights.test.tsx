// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { AdminUsageAggregate } from '@/lib/api/adminUsage';
import {
  getContextInsights,
  type ContextInsightsPage,
  type ConversationContext,
} from '@/lib/api/ownerUsage';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';

import { ContextInsights, PROFILING_RETRY_MS } from './ContextInsights';

vi.mock('@/lib/api/ownerUsage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/ownerUsage')>()),
  getContextInsights: vi.fn(),
}));

const ALL = {
  preset: 'all' as const,
  days: null,
  since: '2023-01-02T00:00:00Z',
  until: '2026-10-15T12:00:00Z',
  day_timezone: 'UTC' as const,
  bucket: 'month' as const,
  first_record_at: '2023-01-02T08:00:00Z',
};

function usage(spent: string | null, cacheRead: number | null): AdminUsageAggregate {
  return {
    run_count: 2,
    attempt_count: 2,
    totals: {
      calls: 4,
      tokens_in: 100_000,
      tokens_out: 1_000,
      cache_read: cacheRead,
      cache_write: 0,
      reasoning: null,
      spent_usd: spent,
    },
    unknown_rows: null,
    estimated_calls: 0,
    oldest_queried_at: null,
    runs_with_snapshot: 2,
    missing_attempts: 0,
    status_counts: { available: 2, partial: 0, stale: 0, pending: 0, unavailable: 0 },
    active_runs: 0,
    refreshing_runs: 0,
  };
}

function row(id: string, overrides: Partial<ConversationContext> = {}): ConversationContext {
  return {
    thread_id: id,
    title: `Conversation ${id}`,
    repo: 'org/repo',
    archived: false,
    latest_job_id: `${id}-latest`,
    turns: 3,
    first_turn_at: '2026-10-14T00:00:00Z',
    last_turn_at: '2026-10-15T09:00:00Z',
    runtimes: ['codex'],
    models: ['gpt-5'],
    attempts: 3,
    attempts_profiled: 3,
    main_requests: 40,
    subagent_requests: 0,
    peak_prompt_tokens: 120_000,
    latest_prompt_tokens: 40_000,
    latest_observed_at: '2026-10-15T09:00:00Z',
    context_window: 272_000,
    compactions: 2,
    usage: usage('1.50', 80_000),
    ...overrides,
  };
}

function page(overrides: Partial<ContextInsightsPage> = {}): ContextInsightsPage {
  return {
    period: ALL,
    sort: 'recent',
    time_basis: 'job_created_at',
    generated_at: '2026-10-15T12:00:00Z',
    summary: {
      conversations: 3,
      conversations_observed: 2,
      main_requests: 60,
      subagent_requests: 7,
      compactions: 2,
      peak_prompt_tokens: 120_000,
      peak_thread_id: 'a',
      deleted_conversation_runs: 4,
    },
    profiling: { pending_attempts: 0, pending_capped: false, refreshed: 0, complete: true },
    conversations: [
      row('a'),
      row('b', {
        title: null,
        context_window: null,
        peak_prompt_tokens: 30_000,
        compactions: 0,
        usage: usage(null, null),
      }),
    ],
    next_cursor: 'next-1',
    ...overrides,
  };
}

function show(range: 'today' | '7d' | '30d' | 'all' = 'all') {
  const onRangeChange = vi.fn();
  const view = render(
    <LocaleProvider>
      <ContextInsights range={range} onRangeChange={onRangeChange} />
    </LocaleProvider>,
  );
  return { onRangeChange, ...view };
}

beforeEach(() => {
  vi.mocked(getContextInsights).mockResolvedValue(page());
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe('ContextInsights', () => {
  it('lists each conversation’s profile and opens its Context view', async () => {
    show();
    const link = await screen.findByRole('link', { name: 'Conversation a' });
    expect(link).toHaveAttribute('href', '/a-latest?view=context');
    const item = link.closest('li')!;
    expect(within(item).getByText('120K · 40K')).toBeInTheDocument();
    expect(within(item).getByText('of a 272K window')).toBeInTheDocument();
    expect(within(item).getByText('$1.50')).toBeInTheDocument();
    expect(within(item).getByText('80%')).toBeInTheDocument();
    expect(getContextInsights).toHaveBeenCalledWith(
      expect.objectContaining({ range: 'all', sort: 'recent', limit: 25 }),
    );
  });

  it('keeps an unrecorded cost, cache hit and window a dash or a label, never zero', async () => {
    show();
    const item = (await screen.findByRole('link', { name: 'Untitled chat' })).closest('li')!;
    expect(within(item).getByText('Window not reported')).toBeInTheDocument();
    expect(within(item).getAllByText('—')).toHaveLength(2);
    expect(within(item).queryByText('$0')).not.toBeInTheDocument();
    expect(within(item).queryByText('0%')).not.toBeInTheDocument();
  });

  it('states the range-wide totals and what is not listed', async () => {
    show();
    expect(await screen.findByText('2 with request sizes recorded')).toBeInTheDocument();
    expect(screen.getByText('7 more by subagents, in their own contexts')).toBeInTheDocument();
    expect(
      screen.getByText(
        '4 turns in deleted conversations count in the usage above but are not listed.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /^Everything since Jan 2, 2023, the first day with a run or container \(UTC\)/,
      ),
    ).toBeInTheDocument();
  });

  it('says the ranking is incomplete and asks again until it is', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
    vi.mocked(getContextInsights)
      .mockResolvedValueOnce(
        page({
          profiling: {
            pending_attempts: 120,
            pending_capped: false,
            refreshed: 40,
            complete: false,
          },
        }),
      )
      .mockResolvedValue(page());
    show();
    expect(
      await screen.findByText(/Still reading 120 runs; numbers and order can change/),
    ).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(PROFILING_RETRY_MS);
    });
    await waitFor(() => expect(getContextInsights).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText(/Still reading/)).not.toBeInTheDocument());
  });

  it('reads another ranking and continues a list from its cursor', async () => {
    show();
    await screen.findByRole('link', { name: 'Conversation a' });
    vi.mocked(getContextInsights).mockResolvedValueOnce(
      page({ conversations: [row('c')], next_cursor: null }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    expect(await screen.findByRole('link', { name: 'Conversation c' })).toBeInTheDocument();
    expect(getContextInsights).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: 'next-1', sort: 'recent' }),
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    fireEvent.click(screen.getByRole('combobox', { name: 'Sort by' }));
    fireEvent.click(
      within(screen.getByRole('listbox')).getByRole('option', { name: 'Largest context' }),
    );
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: 'peak', range: 'all' }),
      ),
    );
  });

  it('changes the shared range from its own control', async () => {
    const { onRangeChange } = show('7d');
    fireEvent.click(await screen.findByRole('combobox', { name: 'Range' }));
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'Today' }));
    expect(onRangeChange).toHaveBeenCalledWith('today');
  });

  it('is empty in words when no conversation has turns in the range', async () => {
    vi.mocked(getContextInsights).mockResolvedValue(
      page({
        conversations: [],
        next_cursor: null,
        summary: { ...page().summary, conversations: 0 },
      }),
    );
    show();
    expect(await screen.findByText('No conversation has turns in this range.')).toBeInTheDocument();
  });

  it('answers in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    show();
    expect(await screen.findByRole('heading', { name: '上下文洞察' })).toBeInTheDocument();
    expect(await screen.findByText('窗口 272K')).toBeInTheDocument();
  });
});
