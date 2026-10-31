// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getContextInsights,
  type ContextInsightsPage,
  type ConversationContext,
} from '@/lib/api/ownerUsage';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';

import { ContextInsights } from './ContextInsights';
import { SEARCH_DEBOUNCE_MS } from './ContextInsightsFilters';

vi.mock('@/lib/api/ownerUsage', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/ownerUsage')>()),
  getContextInsights: vi.fn(),
}));

const WEEK = {
  preset: '7d' as const,
  days: null,
  since: '2026-10-23T00:00:00Z',
  until: '2026-10-29T12:00:00Z',
  day_timezone: 'UTC' as const,
  bucket: 'day' as const,
  first_record_at: null,
};

function row(id: string, repo: string | null): ConversationContext {
  return {
    thread_id: id,
    title: `Conversation ${id}`,
    repo,
    archived: false,
    latest_job_id: `${id}-latest`,
    turns: 2,
    first_turn_at: '2026-10-27T00:00:00Z',
    last_turn_at: '2026-10-28T09:00:00Z',
    runtimes: ['claude-code'],
    models: ['m'],
    attempts: 2,
    attempts_profiled: 2,
    main_requests: 4,
    subagent_requests: 0,
    peak_prompt_tokens: 9_000,
    latest_prompt_tokens: 8_000,
    latest_observed_at: '2026-10-28T09:00:00Z',
    context_window: null,
    compactions: 0,
    usage: null,
    composition: {
      recorded: {
        basis: 'chars_cjk_v1',
        categories: [{ key: 'tool_results', tokens: 1_200 }],
        total_tokens: 1_200,
        turns: 2,
        turns_profiled: 2,
        turns_truncated: 0,
      },
      provider: {
        requests: 4,
        prompt_tokens: 30_000,
        cache_read_tokens: 20_000,
        cache_write_tokens: 0,
        output_tokens: 400,
        output_unreported: 0,
        subagent_requests: 0,
        subagent_prompt_tokens: 0,
        subagent_output_tokens: 0,
      },
    },
  };
}

function page(overrides: Partial<ContextInsightsPage> = {}): ContextInsightsPage {
  return {
    period: WEEK,
    sort: 'recent',
    time_basis: 'job_created_at',
    generated_at: '2026-10-29T12:00:00Z',
    summary: {
      conversations: 2,
      conversations_observed: 2,
      turns: 4,
      main_requests: 8,
      subagent_requests: 0,
      compactions: 0,
      peak_prompt_tokens: 9_000,
      peak_thread_id: 'a',
      deleted_conversation_runs: 0,
    },
    profiling: { pending_attempts: 0, pending_capped: false, refreshed: 0, complete: true },
    conversations: [row('a', 'acme/web'), row('b', null)],
    next_cursor: 'next-1',
    filters: { q: '', repo: null, temporary: false, day: null },
    activity: [
      {
        day: '2026-10-27',
        turns: 1,
        conversations: 1,
        main_requests: 2,
        compactions: 0,
        peak_prompt_tokens: 9_000,
      },
      {
        day: '2026-10-28',
        turns: 3,
        conversations: 2,
        main_requests: 6,
        compactions: 1,
        peak_prompt_tokens: 9_000,
      },
    ],
    projects: [
      {
        repo: 'acme/web',
        conversations: 1,
        turns: 2,
        main_requests: 4,
        compactions: 0,
        peak_prompt_tokens: 9_000,
        last_turn_at: '2026-10-28T09:00:00Z',
      },
      {
        repo: null,
        conversations: 1,
        turns: 2,
        main_requests: 4,
        compactions: 1,
        peak_prompt_tokens: 9_000,
        last_turn_at: '2026-10-28T09:00:00Z',
      },
    ],
    composition: {
      recorded: {
        basis: 'chars_cjk_v1',
        categories: [
          { key: 'turn_prompts', tokens: 600 },
          { key: 'tool_results', tokens: 2_400 },
        ],
        total_tokens: 3_000,
        turns: 4,
        turns_profiled: 3,
        turns_truncated: 1,
      },
      provider: {
        requests: 8,
        prompt_tokens: 60_000,
        cache_read_tokens: 40_000,
        cache_write_tokens: 5_000,
        output_tokens: 800,
        output_unreported: 2,
        subagent_requests: 1,
        subagent_prompt_tokens: 2_000,
        subagent_output_tokens: 10,
      },
    },
    ...overrides,
  };
}

function show(range: '7d' | 'all' = '7d') {
  return render(
    <LocaleProvider>
      <ContextInsights range={range} />
    </LocaleProvider>,
  );
}

function lastFilters() {
  return vi.mocked(getContextInsights).mock.lastCall![0].filters;
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

it('searches the whole range on the server, and later pages keep the search', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
  show();
  const box = await screen.findByRole('searchbox', { name: 'Search conversations' });
  fireEvent.change(box, { target: { value: 'log' } });
  fireEvent.change(box, { target: { value: 'login' } });
  await act(async () => {
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
  });
  await waitFor(() => expect(lastFilters()).toEqual(expect.objectContaining({ q: 'login' })));
  // One read for the whole word, not one per keystroke.
  expect(
    vi.mocked(getContextInsights).mock.calls.filter((call) => call[0].filters?.q === 'log'),
  ).toHaveLength(0);
  expect(await screen.findByText('2 conversations match')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
  await waitFor(() =>
    expect(getContextInsights).toHaveBeenLastCalledWith(
      expect.objectContaining({
        cursor: 'next-1',
        filters: expect.objectContaining({ q: 'login' }),
      }),
    ),
  );
});

it('filters to one project or to the conversations without a repository', async () => {
  show();
  fireEvent.click(await screen.findByRole('combobox', { name: 'Project' }));
  const options = within(screen.getByRole('listbox')).getAllByRole('option');
  expect(options.map((option) => option.textContent)).toEqual([
    'All projects',
    'acme/web (1)',
    'No repository (1)',
  ]);
  fireEvent.click(options[2]);
  await waitFor(() =>
    expect(lastFilters()).toEqual(expect.objectContaining({ repo: null, temporary: true })),
  );
  fireEvent.click(screen.getByRole('combobox', { name: 'Project' }));
  fireEvent.click(
    within(screen.getByRole('listbox')).getByRole('option', { name: 'acme/web (1)' }),
  );
  await waitFor(() =>
    expect(lastFilters()).toEqual(expect.objectContaining({ repo: 'acme/web', temporary: false })),
  );
});

it('groups by project, with the no-repository bucket, and opens one group', async () => {
  show();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Group by project' }));
  expect(screen.queryByRole('link', { name: 'Conversation a' })).not.toBeInTheDocument();
  const groups = screen.getAllByRole('button', { name: 'List conversations' });
  expect(groups).toHaveLength(2);
  const bucket = groups[1].closest('li')!;
  expect(within(bucket).getByText('No repository')).toBeInTheDocument();
  expect(bucket).toHaveTextContent('1 conversations · 2 turns · 4 requests · 1 compactions');
  fireEvent.click(groups[1]);
  await waitFor(() =>
    expect(lastFilters()).toEqual(expect.objectContaining({ temporary: true, repo: null })),
  );
  expect(screen.getByRole('checkbox', { name: 'Group by project' })).not.toBeChecked();
});

it('tells each day of the heatmap and lists the conversations of a chosen day', async () => {
  show();
  const busy = await screen.findByRole('button', {
    name: '2026-10-28: — input tokens over 6 requests, in 3 turns across 2 conversations, 1 compactions',
  });
  expect(screen.getByTitle('2026-10-25: no turns')).toBeInTheDocument();
  fireEvent.click(busy);
  await waitFor(() =>
    expect(lastFilters()).toEqual(expect.objectContaining({ day: '2026-10-28' })),
  );
  expect(busy).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getByRole('button', { name: 'Show every day' }));
  await waitFor(() => expect(lastFilters()).toEqual(expect.objectContaining({ day: null })));
});

it('analyses recorded content only, not the reported input the history already sums', async () => {
  show();
  const chart = await screen.findByRole('region', { name: 'Composition' });
  expect(within(chart).getByText('Recorded content (estimate)')).toBeInTheDocument();
  expect(within(chart).getByText('≈3,000 tokens recorded')).toBeInTheDocument();
  expect(within(chart).getByText('Tool results')).toBeInTheDocument();
  expect(within(chart).getByText('≈2,400 · 80%')).toBeInTheDocument();
  expect(
    within(chart).getByText(
      '3 of 4 turns read · 1 turns have clipped records and are undercounted',
    ),
  ).toBeInTheDocument();
  // The estimate says it is not billed usage and where the reported tokens are.
  expect(within(chart).getByText(/This is not billed usage/)).toHaveTextContent(
    'the tokens and cache hit providers reported are in History above',
  );
  // The page carries the provider's 60K input, 40K cache read and 800 output,
  // but no aggregate of them is drawn here: one bar, the recorded one.
  expect(within(chart).getAllByRole('img')).toHaveLength(1);
  for (const text of [
    /providers reported \(exact\)/,
    /^Input 60K/,
    'Not from cache',
    'Cache read',
  ]) {
    expect(screen.queryByText(text)).not.toBeInTheDocument();
  }
  expect(screen.queryByText(/reported no output/)).not.toBeInTheDocument();
  // No category is a difference between the estimate and the reported input.
  expect(within(chart).queryByText(/System prompt/)).not.toBeInTheDocument();
  // Each conversation carries its own recorded content.
  const item = screen.getByRole('link', { name: 'Conversation a' }).closest('li')!;
  expect(within(item).getByText('≈1,200 recorded')).toBeInTheDocument();
});

it('reads in Chinese', async () => {
  window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
  show();
  expect(await screen.findByRole('searchbox', { name: '搜索会话' })).toBeInTheDocument();
  expect(screen.getByText('每日活动')).toBeInTheDocument();
  expect(screen.getByText('记录的内容（估算）')).toBeInTheDocument();
  expect(screen.getByText(/这不是计费用量/)).toBeInTheDocument();
  expect(screen.queryByText('服务商报告的请求输入（精确值）')).not.toBeInTheDocument();
});
