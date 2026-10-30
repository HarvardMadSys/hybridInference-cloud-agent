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
  type ConversationContext,
  type OwnerContainer,
  type OwnerContainerList,
  type OwnerResourceHistory,
  type OwnerTokenUsage,
  type UsageRange,
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
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
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

  it('offers exactly today, 7 days, 30 days and all time', async () => {
    section();
    fireEvent.click(await screen.findByRole('combobox', { name: 'Range' }));
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
    expect(screen.getByText('已计入输出 Token')).toBeInTheDocument();
    expect(
      await screen.findByText(/^每天从 UTC 零点开始算.*和每日额度重置的时间一致/),
    ).toBeInTheDocument();
  });
});

function conversation(overrides: Partial<ConversationContext> = {}): ConversationContext {
  return {
    thread_id: 'athr_ctx',
    title: 'Long refactor',
    repo: 'example-org/app',
    archived: false,
    latest_job_id: 'job-ctx',
    turns: 4,
    first_turn_at: '2026-10-14T00:00:00Z',
    last_turn_at: '2026-10-15T09:00:00Z',
    runtimes: ['codex'],
    models: ['glm-5.1'],
    attempts: 4,
    attempts_profiled: 4,
    main_requests: 12,
    subagent_requests: 0,
    peak_prompt_tokens: 90_000,
    latest_prompt_tokens: 30_000,
    latest_observed_at: '2026-10-15T09:00:00Z',
    context_window: 200_000,
    compactions: 1,
    usage: aggregate({
      totals: {
        calls: 12,
        tokens_in: 80_000,
        tokens_out: 4_000,
        cache_read: 60_000,
        cache_write: 0,
        reasoning: null,
        spent_usd: '2.5',
      },
    }),
    composition: null,
    ...overrides,
  };
}

/** A week of insights with one conversation, a busy day and both compositions. */
function richInsights(period: UsageRange = WEEK): ContextInsightsPage {
  return insights({
    period,
    summary: {
      ...insights().summary,
      conversations: 1,
      conversations_observed: 1,
      turns: 4,
      main_requests: 12,
      compactions: 1,
      peak_prompt_tokens: 90_000,
      peak_thread_id: 'athr_ctx',
    },
    conversations: [conversation()],
    activity: [
      {
        day: '2026-10-14',
        turns: 4,
        conversations: 1,
        main_requests: 12,
        compactions: 1,
        peak_prompt_tokens: 90_000,
      },
    ],
    composition: {
      recorded: {
        basis: 'chars_cjk_v1',
        categories: [
          { key: 'turn_prompts', tokens: 1_000 },
          { key: 'tool_results', tokens: 3_000 },
        ],
        total_tokens: 4_000,
        turns: 4,
        turns_profiled: 4,
        turns_truncated: 0,
      },
      provider: {
        requests: 12,
        prompt_tokens: 80_000,
        cache_read_tokens: 60_000,
        cache_write_tokens: 2_000,
        output_tokens: 4_000,
        output_unreported: 0,
        subagent_requests: 0,
        subagent_prompt_tokens: 0,
        subagent_output_tokens: 0,
      },
    },
  });
}

async function chooseRange(name: string) {
  fireEvent.click(screen.getByRole('combobox', { name: 'Range' }));
  fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name }));
}

describe('UsageSettings: one range for History and Context insights', () => {
  beforeEach(() => {
    vi.mocked(getContextInsights).mockImplementation(async ({ range }) =>
      richInsights({ ...WEEK, preset: range }),
    );
  });

  it('has exactly one range control, above both sections and below the live containers', async () => {
    section();
    await screen.findByRole('link', { name: 'Long refactor' });
    const controls = screen.getAllByRole('combobox', { name: 'Range' });
    expect(controls).toHaveLength(1);
    const [control] = controls;
    expect(control).toHaveTextContent('Last 7 days');
    const group = screen.getByRole('group', { name: 'Range for History and Context insights' });
    expect(group).toContainElement(control);
    expect(group).toHaveAccessibleDescription(
      'Containers above are live and not limited to this range.',
    );
    const containers = screen.getByRole('region', { name: 'Containers' });
    const history = screen.getByRole('region', { name: 'History' });
    const insightsCard = screen.getByRole('region', { name: 'Context insights' });
    for (const card of [containers, history, insightsCard]) {
      expect(card).not.toContainElement(control);
    }
    // Containers, then the control, then the two sections it applies to.
    const follows = (a: Element, b: Element) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(containers, group)).toBe(true);
    expect(follows(group, history)).toBe(true);
    expect(follows(history, insightsCard)).toBe(true);
    // Sorting stays the insights' own.
    expect(within(insightsCard).getByRole('combobox', { name: 'Sort by' })).toBeInTheDocument();
    // The live containers are read without any range.
    expect(getOwnerContainers).toHaveBeenCalledWith({ limit: 50 });
  });

  it.each([
    ['Today', 'today'],
    ['Last 30 days', '30d'],
    ['All time', 'all'],
  ] as const)('reads History and Context insights for %s together', async (name, range) => {
    section();
    await screen.findByRole('link', { name: 'Long refactor' });
    expect(getOwnerTokens).toHaveBeenLastCalledWith('7d', expect.anything());
    expect(getOwnerResources).toHaveBeenLastCalledWith('7d', expect.anything());
    expect(getContextInsights).toHaveBeenLastCalledWith(expect.objectContaining({ range: '7d' }));
    const containerReads = vi.mocked(getOwnerContainers).mock.calls.length;

    await chooseRange(name);
    await waitFor(() => expect(getOwnerTokens).toHaveBeenLastCalledWith(range, expect.anything()));
    expect(getOwnerResources).toHaveBeenLastCalledWith(range, expect.anything());
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({ range, sort: 'recent' }),
      ),
    );
    expect(screen.getByRole('combobox', { name: 'Range' })).toHaveTextContent(name);
    // The live containers are not read again for a range.
    expect(getOwnerContainers).toHaveBeenCalledTimes(containerReads);
  });

  it('drops a day picked from the heatmap when the range changes, without a stale read', async () => {
    section();
    const day = await screen.findByRole('button', {
      name: '2026-10-14: 4 turns in 1 conversations, 12 requests, 1 compactions',
    });
    fireEvent.click(day);
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({
          range: '7d',
          filters: expect.objectContaining({ day: '2026-10-14' }),
        }),
      ),
    );
    // A day narrows the insights only; History is not read for it.
    expect(getOwnerTokens).toHaveBeenCalledTimes(1);

    await chooseRange('Last 30 days');
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({ range: '30d', filters: expect.objectContaining({ day: null }) }),
      ),
    );
    // No read ever paired the new range with the old day.
    const thirty = vi
      .mocked(getContextInsights)
      .mock.calls.filter(([options]) => options.range === '30d');
    expect(thirty.length).toBeGreaterThan(0);
    for (const [options] of thirty) expect(options.filters?.day).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show every day' })).not.toBeInTheDocument();
    const again = await screen.findByRole('button', {
      name: '2026-10-14: 4 turns in 1 conversations, 12 requests, 1 compactions',
    });
    expect(again).toHaveAttribute('aria-pressed', 'false');
  });

  it('keeps search and sorting to the insights and out of the History reads', async () => {
    section();
    await screen.findByRole('link', { name: 'Long refactor' });
    const historyReads = vi.mocked(getOwnerTokens).mock.calls.length;
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search conversations' }), {
      target: { value: 'refactor' },
    });
    await waitFor(
      () =>
        expect(getContextInsights).toHaveBeenLastCalledWith(
          expect.objectContaining({ filters: expect.objectContaining({ q: 'refactor' }) }),
        ),
      { timeout: 2_000 },
    );
    fireEvent.click(screen.getByRole('combobox', { name: 'Sort by' }));
    fireEvent.click(
      within(screen.getByRole('listbox')).getByRole('option', { name: 'Largest context' }),
    );
    await waitFor(() =>
      expect(getContextInsights).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: 'peak', range: '7d' }),
      ),
    );
    expect(getOwnerTokens).toHaveBeenCalledTimes(historyReads);
    expect(getOwnerResources).toHaveBeenCalledTimes(historyReads);
    expect(screen.getByRole('combobox', { name: 'Range' })).toHaveTextContent('Last 7 days');
  });

  it('keeps the whole History beside a context-only Context insights', async () => {
    const usage = tokens();
    usage.summary.totals = { ...usage.summary.totals!, cache_read: 36_000, reasoning: 5_000 };
    vi.mocked(getOwnerTokens).mockResolvedValue(usage);
    section();
    const history = await screen.findByRole('region', { name: 'History' });
    await waitFor(async () => expect(await tile('Model cost')).toBe('$12.35'));
    // The global consumption metrics are the History's.
    expect(await tile('Tokens')).toBe('150K');
    expect(await tile('Cache hit rate')).toBe('30%');
    expect(await tile('Reasoning tokens')).toBe('5,000');
    expect(await tile('Container running time')).toBe('10 h');
    expect(await tile('CPU')).toBe('2 core-hours');
    expect(await tile('Memory')).toBe('3 GB-hours');
    expect(
      within(history).getByText(
        'CPU and memory cover 4 h of 10 h running time; measurement began recently.',
      ),
    ).toBeInTheDocument();
    // The daily trend still switches metric.
    const metric = within(history).getByRole('group', { name: 'Chart' });
    const running = within(metric).getByRole('button', { name: 'Running hours' });
    fireEvent.click(running);
    expect(running).toHaveAttribute('aria-pressed', 'true');
    expect(within(history).getByRole('table', { name: 'By model' })).toBeInTheDocument();

    const insightsCard = screen.getByRole('region', { name: 'Context insights' });
    // The context analysis: totals, heatmap, recorded composition, one bar.
    expect(within(insightsCard).getByText('Compactions', { selector: 'dt' })).toBeInTheDocument();
    expect(
      within(insightsCard).getByText('Largest request', { selector: 'dt' }),
    ).toBeInTheDocument();
    const composition = within(insightsCard).getByRole('region', { name: 'Composition' });
    expect(within(composition).getByText('≈4,000 tokens recorded')).toBeInTheDocument();
    expect(within(composition).getAllByRole('img')).toHaveLength(1);
    // No second, provider-reported overview of input, output or cache.
    for (const text of [
      /providers reported \(exact\)/,
      /^Input 80K/,
      'Not from cache',
      'Cache read',
      'Cache write',
      'Cache hit rate',
      'Model cost',
    ]) {
      expect(within(insightsCard).queryByText(text)).not.toBeInTheDocument();
    }
    // Each conversation keeps its own cost and cache hit, and opens its Context view.
    const link = within(insightsCard).getByRole('link', { name: 'Long refactor' });
    expect(link).toHaveAttribute('href', '/job-ctx?view=context');
    const item = link.closest('li')!;
    expect(within(item).getByText('$2.50')).toBeInTheDocument();
    expect(within(item).getByText('75%')).toBeInTheDocument();
    expect(within(item).getByText('90K · 30K')).toBeInTheDocument();
  });

  it('names the shared range in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    section();
    await screen.findByRole('link', { name: 'Long refactor' });
    const control = screen.getByRole('combobox', { name: '时间范围' });
    expect(control).toHaveTextContent('最近 7 天');
    expect(screen.getByRole('group', { name: '历史用量和上下文洞察的时间范围' })).toContainElement(
      control,
    );
    expect(screen.getAllByRole('combobox', { name: '时间范围' })).toHaveLength(1);
  });
});
