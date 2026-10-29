// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { NEUTRAL_SITE_CONFIG } from '@/config/site';
import type { AdminUsageAggregate } from '@/lib/api/adminUsage';
import {
  getContextInsights,
  getOwnerContainers,
  getOwnerResources,
  getOwnerTokens,
  type ContextInsightsPage,
  type OwnerContainer,
  type OwnerContainerList,
  type OwnerResourceHistory,
  type OwnerTokenUsage,
} from '@/lib/api/ownerUsage';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';

import { CONTAINER_REFRESH_MS, UsageSettings } from './UsageSettings';

vi.mock('@/lib/api/ownerUsage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/ownerUsage')>()),
  getContextInsights: vi.fn(),
  getOwnerContainers: vi.fn(),
  getOwnerResources: vi.fn(),
  getOwnerTokens: vi.fn(),
}));

const WEEK = {
  preset: '7d' as const,
  days: null,
  since: '2026-10-09T00:00:00Z',
  until: '2026-10-15T12:00:00Z',
  day_timezone: 'UTC' as const,
  bucket: 'day' as const,
  first_record_at: null,
};

function insights(overrides: Partial<ContextInsightsPage> = {}): ContextInsightsPage {
  return {
    period: WEEK,
    sort: 'recent',
    time_basis: 'job_created_at',
    generated_at: '2026-10-15T12:00:00Z',
    summary: {
      conversations: 0,
      conversations_observed: 0,
      turns: 0,
      main_requests: 0,
      subagent_requests: 0,
      compactions: 0,
      peak_prompt_tokens: null,
      peak_thread_id: null,
      deleted_conversation_runs: 0,
    },
    profiling: { pending_attempts: 0, pending_capped: false, refreshed: 0, complete: true },
    conversations: [],
    next_cursor: null,
    filters: { q: '', repo: null, temporary: false, day: null },
    activity: null,
    projects: null,
    composition: null,
    ...overrides,
  };
}

const GIB = 1024 ** 3;
const NOW = Date.parse('2026-10-15T12:00:00Z');

function box(overrides: Partial<OwnerContainer> = {}): OwnerContainer {
  return {
    id: 'asbx_one',
    state: 'running',
    repo: 'example-org/app',
    chats: [{ thread_id: 'athr_one', job_id: 'job-one', title: 'Fix login bug', archived: false }],
    chat_count: 1,
    cpu_limit_cores: 2,
    memory_limit_bytes: 4 * GIB,
    disk_limit_bytes: null,
    reading: {
      cpu_cores: 0.8,
      memory_bytes: 2 * GIB,
      sampled_at: '2026-10-15T11:59:50Z',
      stale: false,
    },
    created_at: '2026-10-15T09:00:00Z',
    last_activity_at: '2026-10-15T11:50:00Z',
    idle_deadline: '2026-10-15T12:18:00Z',
    ...overrides,
  };
}

function list(overrides: Partial<OwnerContainerList> = {}): OwnerContainerList {
  return {
    observed_at: '2026-10-15T12:00:00Z',
    counts: { running: 1, stopped: 1 },
    total_count: 2,
    current: { cpu_cores: 0.8, memory_bytes: 2 * GIB, measured: 1, unmeasured: 0 },
    containers: [
      box(),
      box({
        id: 'asbx_two',
        state: 'stopped',
        repo: 'example-org/api',
        chats: [
          { thread_id: 'athr_two', job_id: 'job-two', title: 'Refactor API', archived: true },
          { thread_id: 'athr_fork', job_id: 'job-fork', title: 'Fork', archived: false },
        ],
        chat_count: 2,
        reading: null,
        idle_deadline: null,
        last_activity_at: '2026-10-15T09:00:00Z',
      }),
    ],
    next_cursor: null,
    ...overrides,
  };
}

function aggregate(overrides: Partial<AdminUsageAggregate> = {}): AdminUsageAggregate {
  return {
    run_count: 3,
    attempt_count: 3,
    totals: {
      calls: 9,
      tokens_in: 120_000,
      tokens_out: 30_000,
      cache_read: 0,
      cache_write: 0,
      reasoning: null,
      spent_usd: '12.3456',
    },
    unknown_rows: null,
    estimated_calls: 0,
    oldest_queried_at: null,
    runs_with_snapshot: 3,
    missing_attempts: 0,
    status_counts: { available: 3, partial: 0, stale: 0, pending: 0, unavailable: 0 },
    active_runs: 0,
    refreshing_runs: 0,
    ...overrides,
  };
}

function tokens(): OwnerTokenUsage {
  return {
    since: '2026-10-09T00:00:00Z',
    until: '2026-10-15T12:00:00Z',
    day_timezone: 'UTC',
    time_basis: 'job_created_at',
    generated_at: '2026-10-15T12:00:00Z',
    summary: aggregate(),
    days: [
      {
        date: '2026-10-15',
        since: '2026-10-15T00:00:00Z',
        until: '2026-10-15T12:00:00Z',
        usage: aggregate(),
      },
    ],
    by_model: {
      group_count: 2,
      groups: [
        { key: 'glm-5.1', usage: aggregate() },
        { key: null, usage: aggregate({ totals: null, run_count: 1 }) },
      ],
      other: null,
    },
    period: WEEK,
    batches: 1,
  };
}

function resources(): OwnerResourceHistory {
  return {
    since: '2026-10-09T00:00:00Z',
    until: '2026-10-15T12:00:00Z',
    day_timezone: 'UTC',
    generated_at: '2026-10-15T12:00:00Z',
    totals: {
      running_seconds: 10 * 3600,
      measured_seconds: 4 * 3600,
      cpu_core_seconds: 2 * 3600,
      memory_byte_seconds: 3 * 3600 * GIB,
    },
    days: [
      {
        date: '2026-10-15',
        since: '2026-10-15T00:00:00Z',
        until: '2026-10-15T12:00:00Z',
        running_seconds: 10 * 3600,
        measured_seconds: 4 * 3600,
        cpu_core_seconds: 2 * 3600,
        memory_byte_seconds: 3 * 3600 * GIB,
      },
    ],
    period: WEEK,
  };
}

/** The value a summary tile shows under its label. */
async function tile(label: string): Promise<string> {
  const name = await screen.findByText(label, { selector: 'dt' });
  return name.parentElement!.querySelector('dd')!.textContent ?? '';
}

function section() {
  return render(
    <SiteConfigProvider
      config={{
        ...NEUTRAL_SITE_CONFIG,
        branding: { ...NEUTRAL_SITE_CONFIG.branding, gatewayUrl: 'https://gateway.example' },
      }}
    >
      <LocaleProvider>
        <UsageSettings />
      </LocaleProvider>
    </SiteConfigProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(NOW);
  vi.mocked(getOwnerContainers).mockResolvedValue(list());
  vi.mocked(getOwnerTokens).mockResolvedValue(tokens());
  vi.mocked(getOwnerResources).mockResolvedValue(resources());
  vi.mocked(getContextInsights).mockResolvedValue(insights());
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe('UsageSettings', () => {
  it('counts the owner’s containers and sums what the running ones use now', async () => {
    section();
    await screen.findByText('Fix login bug');
    expect(screen.getByText('1 running · 1 stopped')).toBeInTheDocument();
    expect(screen.getByText('0.8 cores')).toBeInTheDocument();
    expect(screen.getByText('2 GB')).toBeInTheDocument();
    expect(screen.getByText('CPU 0.8 / 2 cores · Memory 2 GB / 4 GB')).toBeInTheDocument();
    expect(screen.getByText('Stops when idle in 18 minutes')).toBeInTheDocument();
  });

  it('links each container to its chat and names archived and shared chats', async () => {
    section();
    const link = await screen.findByRole('link', { name: 'Refactor API' });
    expect(link).toHaveAttribute('href', '/job-two');
    const row = link.closest('li')!;
    expect(within(row).getByText('Archived')).toBeInTheDocument();
    expect(within(row).getByText('+1 more')).toBeInTheDocument();
    expect(within(row).getByText('Limit 2 cores · 4 GB')).toBeInTheDocument();
    expect(within(row).getByText('Last used 3 hours ago')).toBeInTheDocument();
  });

  it('continues the list from its cursor and keeps the longer list on refresh', async () => {
    vi.mocked(getOwnerContainers)
      .mockResolvedValueOnce(list({ next_cursor: 'cursor-1', total_count: 3 }))
      .mockResolvedValueOnce(
        list({ containers: [box({ id: 'asbx_three', chats: [], repo: 'example-org/three' })] }),
      );
    section();
    fireEvent.click(await screen.findByRole('button', { name: 'Show 1 more' }));
    await screen.findByText('example-org/three');
    expect(getOwnerContainers).toHaveBeenLastCalledWith({ cursor: 'cursor-1', limit: 50 });
    expect(screen.queryByRole('button', { name: /Show/ })).not.toBeInTheDocument();
  });

  it('keeps every shown container when a refresh reads past one page', async () => {
    // One page holds at most 100 containers; this owner has 150 and has shown
    // all of them.
    let read = 'repo';
    vi.mocked(getOwnerContainers).mockImplementation(async ({ cursor, limit = 50 } = {}) => {
      const start = cursor ? Number(cursor) : 0;
      const end = Math.min(150, start + Math.min(limit, 100));
      return list({
        total_count: 150,
        containers: Array.from({ length: end - start }, (_, offset) =>
          box({ id: `asbx_${start + offset}`, chats: [], repo: `${read}-${start + offset}` }),
        ),
        next_cursor: end < 150 ? String(end) : null,
      });
    });
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    section();
    fireEvent.click(await screen.findByRole('button', { name: 'Show 100 more' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Show 50 more' }));
    await screen.findByText('repo-149');

    read = 'fresh';
    await act(async () => {
      vi.advanceTimersByTime(CONTAINER_REFRESH_MS);
    });
    await screen.findByText('fresh-0');
    expect(screen.getAllByRole('listitem')).toHaveLength(150);
    expect(screen.getByText('fresh-149')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Show/ })).not.toBeInTheDocument();
  });

  it('reads the containers again while the page is visible', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    section();
    await screen.findByText('Fix login bug');
    expect(getOwnerContainers).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(CONTAINER_REFRESH_MS);
    });
    await waitFor(() => expect(getOwnerContainers).toHaveBeenCalledTimes(2));
    expect(getOwnerContainers).toHaveBeenLastCalledWith({ limit: 50 });
  });

  it('shows the period’s cost, tokens and container time, and how much was measured', async () => {
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    expect(screen.getByText('Estimate, not a bill')).toBeInTheDocument();
    expect(
      screen.getByText(/^Days start at 00:00 UTC.*when the daily limit resets/),
    ).toBeInTheDocument();
    expect(await tile('Tokens')).toBe('150K');
    expect(screen.getByText('120K in · 30K out')).toBeInTheDocument();
    expect(screen.getByText('10 h')).toBeInTheDocument();
    expect(screen.getByText('2 core-hours')).toBeInTheDocument();
    expect(screen.getByText('3 GB-hours')).toBeInTheDocument();
    expect(
      screen.getByText(
        'CPU and memory cover 4 h of 10 h running time; measurement began recently.',
      ),
    ).toBeInTheDocument();
    const table = screen.getByRole('table', { name: 'By model' });
    expect(within(table).getByText('glm-5.1')).toBeInTheDocument();
    expect(within(table).getByText('Unknown model')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Daily limit and account usage' })).toHaveAttribute(
      'href',
      'https://gateway.example/dashboard',
    );
  });

  it('shows the cache hit rate and reasoning tokens apart from the token total', async () => {
    const usage = tokens();
    usage.summary.totals = { ...usage.summary.totals!, cache_read: 36_000, reasoning: 5_000 };
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    await waitFor(async () => expect(await tile('Cache hit rate')).toBe('30%'));
    expect(screen.getByText('36K of 120K input from cache')).toBeInTheDocument();
    expect(await tile('Reasoning tokens')).toBe('5,000');
    expect(screen.getByText('Not added to Tokens')).toBeInTheDocument();
    expect(await tile('Tokens')).toBe('150K');
  });

  it('reads a cache hit rate or reasoning it cannot know as a dash, not zero', async () => {
    const usage = tokens();
    usage.summary.unknown_rows = {
      tokens_in: 0,
      tokens_out: 0,
      cache_read: 2,
      cache_write: 0,
      reasoning: 0,
      spent_usd: 0,
    };
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    expect(await tile('Cache hit rate')).toBe('—');
    expect(await tile('Reasoning tokens')).toBe('—');
  });

  it('says what keeps the model numbers from being final', async () => {
    const usage = tokens();
    usage.summary = aggregate({
      run_count: 5,
      active_runs: 1,
      estimated_calls: 2,
      oldest_queried_at: '2026-10-15T09:00:00Z',
      status_counts: { available: 2, partial: 1, stale: 0, pending: 1, unavailable: 1 },
      unknown_rows: {
        tokens_in: 0,
        tokens_out: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: 0,
        spent_usd: 4,
      },
    });
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    const note = await screen.findByRole('note', {
      name: 'These model numbers are not final yet.',
    });
    expect(within(note).getByText('Turns still running: 1')).toBeInTheDocument();
    expect(within(note).getByText('Turns not fully counted: 3')).toBeInTheDocument();
    expect(within(note).getByText('Model calls without a recorded cost: 4')).toBeInTheDocument();
    expect(within(note).getByText('Model calls with estimated tokens: 2')).toBeInTheDocument();
    expect(
      within(note).getByText(
        'Usage is read from a cache; the oldest read in these numbers was 3 hours ago.',
      ),
    ).toBeInTheDocument();
  });

  it('shows no note when the model numbers have nothing outstanding', async () => {
    const usage = tokens();
    usage.summary = aggregate({ oldest_queried_at: '2026-10-15T09:00:00Z' });
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    // The range note stays; the not-final note does not appear.
    expect(
      screen.queryByRole('note', { name: 'These model numbers are not final yet.' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/not final/)).not.toBeInTheDocument();
  });

  it('reads both histories and the insights again for another range', async () => {
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    expect(getOwnerTokens).toHaveBeenLastCalledWith('7d', expect.anything());
    const [history] = screen.getAllByRole('combobox', { name: 'Range' });
    fireEvent.click(history);
    fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: 'All time' }));
    await waitFor(() => expect(getOwnerTokens).toHaveBeenLastCalledWith('all', expect.anything()));
    expect(getOwnerResources).toHaveBeenLastCalledWith('all', expect.anything());
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({ range: 'all', sort: 'recent' }),
      ),
    );
    // Both cards show the one shared range.
    for (const control of screen.getAllByRole('combobox', { name: 'Range' })) {
      expect(control).toHaveTextContent('All time');
    }
  });

  it('offers exactly today, 7 days, 30 days and all time', async () => {
    section();
    const [history] = await screen.findAllByRole('combobox', { name: 'Range' });
    fireEvent.click(history);
    const options = within(screen.getByRole('listbox')).getAllByRole('option');
    expect(options.map((option) => option.textContent)).toEqual([
      'Today',
      'Last 7 days',
      'Last 30 days',
      'All time',
    ]);
  });

  it('names the range it shows in UTC days, and all time from its first record', async () => {
    const all = {
      ...WEEK,
      preset: 'all' as const,
      since: '2024-03-09T00:00:00Z',
      bucket: 'week' as const,
      first_record_at: '2024-03-09T17:30:00Z',
    };
    vi.mocked(getOwnerTokens).mockResolvedValue({ ...tokens(), period: all });
    vi.mocked(getOwnerResources).mockResolvedValue({ ...resources(), period: all });
    section();
    expect(
      await screen.findByText(
        'Everything since Mar 9, 2024, the first UTC day with a run or container. One bar per week, Monday to Sunday.',
      ),
    ).toBeInTheDocument();
  });

  it('shows a refusal in the reader’s words', async () => {
    const { APIError } = await import('@/lib/utils/errors');
    vi.mocked(getOwnerTokens).mockRejectedValue(
      new APIError('usage_temporarily_unavailable', 'Usage could not be read right now.', 503),
    );
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    section();
    expect(await screen.findByText('暂时无法读取用量，请重试。')).toBeInTheDocument();
  });

  it.each([
    ['usage_query_too_large', 422, 'This usage is too large to sum. Choose a shorter range.'],
    ['usage_response_too_large', 422, 'This usage is too large to send. Choose a shorter range.'],
    ['something_new', 503, 'Usage could not be read right now. Try again.'],
    ['something_new', 400, 'Could not load usage.'],
  ])('words a %s refusal itself, never the server’s prose', async (code, status, words) => {
    const { APIError } = await import('@/lib/utils/errors');
    vi.mocked(getOwnerTokens).mockRejectedValue(new APIError(code, 'Server prose.', status));
    section();
    expect(await screen.findByText(words)).toBeInTheDocument();
    expect(screen.queryByText('Server prose.')).not.toBeInTheDocument();
  });

  it('keeps container history when model usage cannot be read', async () => {
    vi.mocked(getOwnerTokens).mockRejectedValue(new Error('Too many runs to sum.'));
    section();
    expect(await screen.findByText('Could not load usage.')).toBeInTheDocument();
    expect(await screen.findByText('10 h')).toBeInTheDocument();
    vi.mocked(getOwnerTokens).mockResolvedValue(tokens());
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
  });

  it('answers in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    section();
    expect(await screen.findByRole('heading', { name: '容器' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '历史用量' })).toBeInTheDocument();
    expect(await screen.findByText('1 个运行中 · 1 个已停止')).toBeInTheDocument();
    expect(screen.getByText('最后使用：3小时前')).toBeInTheDocument();
    expect(await screen.findByText('估算，不是账单')).toBeInTheDocument();
    expect(await tile('缓存命中率')).toBe('0%');
    expect(await tile('推理 Token')).toBe('—');
    expect(screen.getByText('不计入 Token 合计')).toBeInTheDocument();
    expect(
      await screen.findByText(/^每天从 UTC 零点开始算.*和每日额度重置的时间一致/),
    ).toBeInTheDocument();
  });
});
