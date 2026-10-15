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
  type OwnerResourceHistory,
  type OwnerTokenUsage,
} from '@/lib/api/ownerUsage';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';

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

  it('reads both histories again for another period', async () => {
    section();
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    fireEvent.click(screen.getByRole('button', { name: '30 days' }));
    await waitFor(() => expect(getOwnerTokens).toHaveBeenLastCalledWith(30, expect.anything()));
    expect(getOwnerResources).toHaveBeenLastCalledWith(30, expect.anything());
    expect(screen.getByRole('button', { name: '30 days' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('keeps container history when model usage cannot be read', async () => {
    vi.mocked(getOwnerTokens).mockRejectedValue(new Error('Too many runs to sum.'));
    section();
    expect(await screen.findByText('Too many runs to sum.')).toBeInTheDocument();
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
    expect(
      await screen.findByText(/^每天从 UTC 零点开始算.*和每日额度重置的时间一致/),
    ).toBeInTheDocument();
  });
});
