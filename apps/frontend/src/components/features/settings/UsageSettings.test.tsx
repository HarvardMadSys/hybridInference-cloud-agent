// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { NEUTRAL_SITE_CONFIG } from '@/config/site';
import type { AdminUsageAggregate } from '@/lib/api/adminUsage';
import {
  getOwnerContainers,
  getOwnerResources,
  getOwnerTokens,
  type OwnerContainer,
  type OwnerContainerList,
  type OwnerHistoryPeriod,
  type OwnerResourceHistory,
  type OwnerTokenUsage,
} from '@/lib/api/ownerUsage';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { APIError } from '@/lib/utils/errors';

import { CONTAINER_REFRESH_MS, UsageSettings } from './UsageSettings';

vi.mock('@/lib/api/ownerUsage', () => ({
  getOwnerContainers: vi.fn(),
  getOwnerResources: vi.fn(),
  getOwnerTokens: vi.fn(),
}));

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

const WEEK: OwnerHistoryPeriod = {
  kind: 'days',
  since: '2026-10-09T00:00:00Z',
  until: '2026-10-15T12:00:00Z',
  first_activity_at: null,
  limited_by: null,
};

function tokens(): OwnerTokenUsage {
  return {
    since: '2026-10-09T00:00:00Z',
    until: '2026-10-15T12:00:00Z',
    day_timezone: 'UTC',
    time_basis: 'job_created_at',
    generated_at: '2026-10-15T12:00:00Z',
    period: WEEK,
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
  };
}

function resources(): OwnerResourceHistory {
  return {
    since: '2026-10-09T00:00:00Z',
    until: '2026-10-15T12:00:00Z',
    day_timezone: 'UTC',
    generated_at: '2026-10-15T12:00:00Z',
    period: WEEK,
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
  };
}

/** The value a summary tile shows under its label. */
async function tile(label: string): Promise<string> {
  const name = await screen.findByText(label, { selector: 'dt' });
  return name.parentElement!.querySelector('dd')!.textContent ?? '';
}

function section() {
  return render(
    <SiteConfigProvider config={NEUTRAL_SITE_CONFIG}>
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

  it('shows the period’s tokens and container time, and leaves cost to the provider', async () => {
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('Your provider'));
    expect(screen.getByText(/bills and limits its use/)).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).toBeNull();
    expect(screen.getByText(/^Days start at 00:00 UTC/)).toBeInTheDocument();
    expect(screen.queryByText(/daily limit/)).toBeNull();
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
    expect(screen.queryByRole('link', { name: /Daily limit/ })).toBeNull();
  });

  it('shows the cache hit rate, and reasoning tokens as a part of output', async () => {
    const usage = tokens();
    usage.summary.totals = { ...usage.summary.totals!, cache_read: 36_000, reasoning: 5_000 };
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    await waitFor(async () => expect(await tile('Cache hit rate')).toBe('30%'));
    expect(screen.getByText('36K of 120K input from cache')).toBeInTheDocument();
    expect(await tile('Reasoning tokens')).toBe('5,000');
    expect(screen.getByText('Included in output tokens')).toBeInTheDocument();
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
    await waitFor(async () => expect(await tile('Model cost')).toBe('Your provider'));
    expect(await tile('Cache hit rate')).toBe('—');
    expect(await tile('Reasoning tokens')).toBe('—');
  });

  it('says when reasoning is only partly reported, still as a part of output', async () => {
    const usage = tokens();
    usage.summary.totals = { ...usage.summary.totals!, reasoning: 5_000 };
    usage.summary.unknown_rows = {
      tokens_in: 0,
      tokens_out: 0,
      cache_read: 0,
      cache_write: 0,
      reasoning: 3,
      spent_usd: 0,
    };
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    await waitFor(async () => expect(await tile('Reasoning tokens')).toBe('5,000'));
    expect(
      screen.getByText('Some calls did not report it · included in output tokens'),
    ).toBeInTheDocument();
    expect(await tile('Tokens')).toBe('150K');
  });

  it('reads both histories again for another period', async () => {
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('Your provider'));
    expect(getOwnerTokens).toHaveBeenLastCalledWith({ days: 7 }, expect.anything());
    const periods = screen.getByRole('group', { name: 'Period' });
    expect(
      within(periods)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual(['Today', '7 days', '30 days', 'All']);
    fireEvent.click(screen.getByRole('button', { name: '30 days' }));
    await waitFor(() =>
      expect(getOwnerTokens).toHaveBeenLastCalledWith({ days: 30 }, expect.anything()),
    );
    expect(getOwnerResources).toHaveBeenLastCalledWith({ days: 30 }, expect.anything());
    expect(screen.getByRole('button', { name: '30 days' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() =>
      expect(getOwnerTokens).toHaveBeenLastCalledWith({ all: true }, expect.anything()),
    );
    expect(getOwnerResources).toHaveBeenLastCalledWith({ all: true }, expect.anything());
  });

  it('draws no chart for today, only its totals', async () => {
    const yesterday = {
      date: '2026-10-14',
      since: '2026-10-14T00:00:00Z',
      until: '2026-10-15T00:00:00Z',
    };
    vi.mocked(getOwnerTokens).mockResolvedValue({
      ...tokens(),
      days: [{ ...tokens().days[0], ...yesterday }, ...tokens().days],
    });
    vi.mocked(getOwnerResources).mockResolvedValue({
      ...resources(),
      days: [{ ...resources().days[0], ...yesterday }, ...resources().days],
    });
    section();
    expect(await screen.findByRole('region', { name: 'Tokens by day' })).toBeInTheDocument();
    vi.mocked(getOwnerTokens).mockResolvedValue(tokens());
    vi.mocked(getOwnerResources).mockResolvedValue(resources());
    fireEvent.click(screen.getByRole('button', { name: 'Today' }));
    await waitFor(() =>
      expect(getOwnerTokens).toHaveBeenLastCalledWith({ days: 1 }, expect.anything()),
    );
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Tokens by day' })).not.toBeInTheDocument(),
    );
    expect(await tile('Model cost')).toBe('Your provider');
  });

  it('says where a narrowed all-time period starts, and sums weeks', async () => {
    const period: OwnerHistoryPeriod = {
      kind: 'all',
      since: '2026-08-10T00:00:00Z',
      until: '2026-10-15T12:00:00Z',
      first_activity_at: '2026-02-01T09:00:00Z',
      limited_by: 'runs',
    };
    const start = Date.parse(period.since);
    const dates = Array.from({ length: 67 }, (_, index) =>
      new Date(start + index * 86_400_000).toISOString().slice(0, 10),
    );
    vi.mocked(getOwnerTokens).mockResolvedValue({
      ...tokens(),
      since: period.since,
      period,
      days: dates.map((date) => ({ ...tokens().days[0], date, since: `${date}T00:00:00Z` })),
    });
    vi.mocked(getOwnerResources).mockResolvedValue({
      ...resources(),
      since: period.since,
      period,
      days: dates.map((date) => ({ ...resources().days[0], date, since: `${date}T00:00:00Z` })),
    });
    section();
    fireEvent.click(await screen.findByRole('button', { name: 'All' }));
    const note = await screen.findByRole('note');
    expect(note).toHaveTextContent(
      'Since 2026-08-10 · Earlier usage is more than one summary can add up and is not included.',
    );
    const chart = await screen.findByRole('region', { name: 'Tokens by week' });
    // Monday 2026-08-10 to Thursday 2026-10-15: nine whole weeks and a partial one.
    expect(within(chart).getAllByRole('row')).toHaveLength(10);
  });

  it('keeps container history when model usage cannot be read', async () => {
    vi.mocked(getOwnerTokens).mockRejectedValue(
      new APIError('usage_query_too_large', 'Too many runs to sum.', 422),
    );
    section();
    expect(
      await screen.findByText(
        'This period has too many turns to add up at once. Choose a shorter period.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('Too many runs to sum.')).not.toBeInTheDocument();
    expect(await screen.findByText('10 h')).toBeInTheDocument();
    vi.mocked(getOwnerTokens).mockResolvedValue(tokens());
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(async () => expect(await tile('Model cost')).toBe('Your provider'));
  });

  it('answers in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    section();
    expect(await screen.findByRole('heading', { name: '容器' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '历史用量' })).toBeInTheDocument();
    expect(await screen.findByText('1 个运行中 · 1 个已停止')).toBeInTheDocument();
    expect(screen.getByText('最后使用：3小时前')).toBeInTheDocument();
    expect(await screen.findByText('由服务商计费')).toBeInTheDocument();
    expect(await tile('缓存命中率')).toBe('0%');
    expect(await tile('推理 Token')).toBe('—');
    expect(screen.getByText('智能体不上报此项')).toBeInTheDocument();
    expect(await screen.findByText(/^每天从 UTC 零点开始算/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '今天' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '全部' })).toBeInTheDocument();
  });

  it('tells a refusal in Chinese rather than the server’s English', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    vi.mocked(getOwnerResources).mockRejectedValue(
      new APIError('usage_temporarily_unavailable', 'Usage could not be read right now.', 503),
    );
    section();
    expect(await screen.findByText('暂时无法读取用量，请稍后再试。')).toBeInTheDocument();
    expect(screen.queryByText(/could not be read/)).not.toBeInTheDocument();
  });
});
