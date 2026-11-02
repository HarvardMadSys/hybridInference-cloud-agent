// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentFileActivity,
  getAgentFileOperations,
  type FileActivityApi,
  getAgentThreadContext,
  getEventContent,
  getRequestComposition,
  getThreadContextCommands,
  getThreadContextDna,
  getThreadContextTiming,
  type ContextPointApi,
  type RequestCompositionApi,
  type ThreadContextApi,
} from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import type { AgentJob } from '../types';
import { ContextView } from './ContextView';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentThreadContext: vi.fn(),
  getRequestComposition: vi.fn(),
  getEventContent: vi.fn(),
  getThreadContextTiming: vi.fn(),
  getThreadContextDna: vi.fn(),
  getThreadContextCommands: vi.fn(),
  // The File Activity section reads its own page; absent is enough here.
  getAgentFileActivity: vi.fn().mockResolvedValue(null),
  getAgentFileOperations: vi.fn().mockResolvedValue(null),
}));

function point(
  index: number,
  prompt: number,
  overrides: Partial<ContextPointApi> = {},
): ContextPointApi {
  return {
    index,
    job_id: index < 2 ? 'j1' : 'j2',
    turn_no: index < 2 ? 1 : 2,
    attempt_id: index < 2 ? 1 : 2,
    seq: index + 1,
    event_id: 500 + index,
    at: '2026-10-15T11:00:00Z',
    prompt_tokens: prompt,
    cache_read_tokens: 1000,
    cache_write_tokens: 0,
    uncached_input_tokens: 10,
    output_tokens: 20,
    context_window: null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
    ...overrides,
  };
}

function view(overrides: Partial<ThreadContextApi> = {}): ThreadContextApi {
  const requests = [
    point(0, 1_010),
    point(1, 60_010, { delta_tokens: 59_000 }),
    point(2, 8_010, { delta_tokens: -52_000, after_compaction: true }),
    point(3, 2_010, { delta_tokens: -6_000 }),
  ];
  return {
    thread_id: 'athr_1',
    generated_at: '2026-10-15T12:00:00Z',
    capabilities: [
      {
        runtime: 'claude-code',
        turns: 2,
        request_usage: 'reported',
        subagent_split: 'reported',
        compaction_events: 'reported',
        compaction_sizes: 'partly_reported',
        context_window: 'partly_reported',
        window_reported_turns: 0,
        native_composition: 'names_only',
        snapshot_turns: 1,
        live_occupancy: 'not_collected',
      },
    ],
    latest: { point: requests[3], window_share: null, compacted_after: false, turn_active: false },
    peak: requests[1],
    turns: [
      {
        job_id: 'j1',
        turn_no: 1,
        created_at: null,
        runtime: 'claude-code',
        model: 'm',
        state: 'succeeded',
        copied: false,
        attempts: 1,
        requests: 2,
        subagent_requests: 0,
        first_prompt_tokens: 1_010,
        last_prompt_tokens: 60_010,
        peak_prompt_tokens: 60_010,
        change_tokens: null,
        compactions: 0,
        context_window: null,
        first_index: 0,
      },
      {
        job_id: 'j2',
        turn_no: 2,
        created_at: null,
        runtime: 'claude-code',
        model: 'm',
        state: 'succeeded',
        copied: false,
        attempts: 1,
        requests: 2,
        subagent_requests: 0,
        first_prompt_tokens: 8_010,
        last_prompt_tokens: 2_010,
        peak_prompt_tokens: 8_010,
        change_tokens: -58_000,
        compactions: 1,
        context_window: null,
        first_index: 2,
      },
    ],
    requests,
    compactions: [
      {
        job_id: 'j2',
        turn_no: 2,
        attempt_id: 2,
        seq: 1,
        event_id: 777,
        at: '2026-10-15T11:30:00Z',
        source: 'claude_compact_boundary',
        trigger: 'auto',
        pre_tokens: 60_000,
        post_tokens: null,
        completed: true,
        observed_before: 60_010,
        observed_after: 8_010,
      },
    ],
    largest_changes: [
      {
        direction: 'growth',
        delta_tokens: 59_000,
        from_index: 0,
        to_index: 1,
        compaction_between: false,
      },
      {
        direction: 'drop',
        delta_tokens: -52_000,
        from_index: 1,
        to_index: 2,
        compaction_between: true,
      },
      {
        direction: 'drop',
        delta_tokens: -6_000,
        from_index: 2,
        to_index: 3,
        compaction_between: false,
      },
    ],
    coverage: {
      turns: 2,
      attempts: 2,
      attempts_observed: 2,
      main_requests: 4,
      subagent_requests: 3,
      compactions: 1,
      compactions_unknown_outcome: 0,
      points_omitted: 0,
      unparsed_raw: 0,
    },
    events: [],
    event_counts: {},
    events_omitted: 0,
    event_capabilities: [],
    ...overrides,
  };
}

const JOB = { id: 'j2', state: 'succeeded', conversationState: 'idle' } as unknown as AgentJob;

function show(onOpenPoint = vi.fn(), onOpenTurn = vi.fn()) {
  render(
    <LocaleProvider>
      <ContextView job={JOB} onOpenPoint={onOpenPoint} onOpenTurn={onOpenTurn} />
    </LocaleProvider>,
  );
  return { onOpenPoint, onOpenTurn };
}

async function fact(label: string): Promise<HTMLElement> {
  const name = await screen.findByText(label, { selector: 'dt' });
  return name.parentElement!;
}

function composition(
  eventId: number,
  overrides: Partial<RequestCompositionApi> = {},
): RequestCompositionApi {
  const request = view().requests.find((p) => p.event_id === eventId)!;
  return {
    thread_id: 'athr_1',
    request,
    runtime: 'claude-code',
    native: {
      v: 1,
      source: 'claude_init',
      status: 'ok',
      token_basis: 'names_only',
      total_tokens: null,
      context_window: null,
      categories: [
        { key: 'tool_definitions', tokens: null, entries: 2, exposed: true },
        { key: 'mcp_tools', tokens: null, entries: 1, exposed: true },
        { key: 'system_prompt', tokens: null, exposed: false },
      ],
      entries: [
        { category: 'tool_definitions', label: 'Bash', tokens: null, token_basis: 'names_only' },
        {
          category: 'mcp_tools',
          label: 'mcp__github__search_code',
          source: 'github',
          tokens: null,
          token_basis: 'names_only',
        },
      ],
      entries_omitted: 0,
      mcp_servers: [{ name: 'github', status: 'connected' }],
    },
    native_relation: { event_id: 900, requests_before: 1, compacted_since: true },
    native_status: 'ok',
    reconstructed: {
      basis: 'stored_events_transcript_order',
      token_basis: 'chars_cjk_v1',
      categories: [
        { key: 'user_messages', entries: 1, chars: 8, tokens: 6, truncated_entries: 0 },
        { key: 'tool_results', entries: 1, chars: 4000, tokens: 1000, truncated_entries: 1 },
      ],
      entries: [
        {
          category: 'tool_results',
          label: 'Bash',
          event_id: 777,
          job_id: 'j2',
          turn_no: 2,
          chars: 4000,
          tokens: 1000,
          truncated: true,
          preview: 'line line line',
        },
        {
          category: 'user_messages',
          label: 'prompt',
          event_id: null,
          job_id: 'j2',
          turn_no: 2,
          chars: 8,
          tokens: 6,
          preview: '继续 continue',
          truncated: false,
        },
      ],
      entries_total: 2,
      entries_omitted: 0,
      estimated_tokens: 1006,
      boundary: {
        job_id: 'j2',
        turn_no: 2,
        seq: 1,
        event_id: 776,
        source: 'cli_compact_boundary',
        may_preserve_recent: true,
      },
      excluded: {
        compacted: { entries: 6, chars: 900 },
        superseded_attempts: { entries: 0, chars: 0 },
        subagent: { entries: 1, chars: 10 },
        this_request_output: { entries: 0, chars: 0 },
      },
      copied_turns: 0,
      events_capped: false,
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(getAgentThreadContext).mockResolvedValue(view());
  vi.mocked(getThreadContextTiming).mockResolvedValue(null);
  vi.mocked(getThreadContextDna).mockResolvedValue(null);
  vi.mocked(getThreadContextCommands).mockResolvedValue({
    thread_id: 't',
    generated_at: '2026-10-29T12:00:00Z',
    slow_cutoff_ms: 10_000,
    summary: {
      total: 0,
      settled: 0,
      failed: 0,
      failure_rate: null,
      running: 0,
      unknown: 0,
      cumulative_ms: 0,
      durations_known: 0,
      durations_missing: 0,
      duration_basis: 'none',
      background: 0,
    },
    coverage: {
      executions_total: 0,
      executions_covered: 0,
      truncated: false,
      turns: 0,
      attempts: 0,
      copied_jobs_excluded: 0,
      unsupported_runtimes: [],
      results_partial: false,
    },
    counts: { all: 0, failed: 0, slow: 0, background: 0 },
    attention: { slowest: null, failing: null, repeats: null, background: 0 },
    programs: [],
    executions: [],
    page: { total: 0, returned: 0, limit: 100, next_cursor: null },
    filter: 'all',
    sort: 'latest',
    q: '',
    program: null,
  });
  vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
    composition(eventId),
  );
  // resetAllMocks() below drops every implementation, so this section's read
  // is restored with the rest on each test.
  vi.mocked(getAgentFileActivity).mockResolvedValue(null);
  vi.mocked(getAgentFileOperations).mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
});

function card(title: string): HTMLElement {
  return screen.getByRole('heading', { name: title }).closest('section')!;
}

function trendGroup(): HTMLElement {
  return screen.getByRole('group', { name: /Context of \d+ columns/ });
}

function strip(): HTMLElement {
  return screen.getByRole('group', { name: 'Request details' });
}

/** The blocks the page stacked under the dashboard before it was one flow. */
const OLD_HEADINGS = [
  'Compactions',
  'Largest changes',
  'Request sizes (as reported)',
  'Turns',
  'Request details',
  'What each agent reports',
];

describe('ContextView', () => {
  it('reads top to bottom: counts, tokens and time, window and trend, browser, events', async () => {
    show();
    await screen.findByText('Context Browser');
    const order = [
      'Token Stats',
      'Timing Stats',
      'Latest observed request',
      'Context Trend',
      'Context Browser',
      'Context events',
    ].map((name) => screen.getByRole('heading', { name }));
    for (let i = 1; i < order.length; i += 1) {
      expect(order[i - 1].compareDocumentPosition(order[i])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }
    // The overview line comes first and is one line of facts, not a wall of cards.
    const requests = await fact('Model requests');
    expect(requests.compareDocumentPosition(order[0])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // Three facts on that one line; the sections below have terms of their own.
    expect(within(requests.parentElement!).getAllByRole('term')).toHaveLength(3);
    for (const name of OLD_HEADINGS) {
      expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument();
    }
    // The old raw chart, KPI facts and turns table are gone, not collapsed.
    expect(
      screen.queryByRole('img', { name: 'Input tokens of each model request' }),
    ).not.toBeInTheDocument();
    for (const label of ['Latest observed context', 'Context window', 'Compactions']) {
      expect(screen.queryByText(label, { selector: 'dt' })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '#1' })).not.toBeInTheDocument();
  });

  it('names the latest request as observed, never as the live window', async () => {
    show();
    await screen.findByRole('heading', { name: 'Latest observed request' });
    const latest = card('Latest observed request');
    expect(within(latest).getByText('2,010')).toBeInTheDocument();
    // No window reported: no share, and no 0%.
    expect(
      within(latest).getByText('The window size was not reported, so no share is shown.'),
    ).toBeInTheDocument();
    expect(within(latest).queryByText(/%$/)).not.toBeInTheDocument();
    expect(within(await fact('Live occupancy')).getByText('Not collected')).toBeInTheDocument();
    expect(
      within(await fact('Model requests')).getByText(
        '4 · 3 more by subagents, in their own contexts',
      ),
    ).toBeInTheDocument();
    expect(within(await fact('Largest request')).getByText('60K · Turn 1')).toBeInTheDocument();
  });

  it('shows a reported window as a share of it', async () => {
    const data = view();
    const reported = { ...data.latest!.point, context_window: 200_000 };
    data.requests = [...data.requests.slice(0, 3), reported];
    data.latest = { ...data.latest!, window_share: 0.01, point: reported };
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    show();
    const latest = await vi.waitFor(() => card('Latest observed request'));
    expect(within(latest).getByText('1.0%')).toBeInTheDocument();
    expect(within(latest).getByText('198K left in the reported window')).toBeInTheDocument();
  });

  it('warns once when a compaction came after the latest observation', async () => {
    const data = view();
    data.latest = { ...data.latest!, compacted_after: true };
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    show();
    expect(await screen.findAllByText(/compacted the context after this request/)).toHaveLength(1);
  });

  it('marks only reported compactions on the trend, and calls a later drop a drop', async () => {
    show();
    await screen.findByText('Context Browser');
    const trend = card('Context Trend');
    // One reported compaction, one marker, at request 3; request 4's drop has none.
    expect(trend.querySelectorAll('path.fill-rose-400')).toHaveLength(1);
    expect(within(strip()).getByText('Request 4 · turn 2 · 2,010')).toBeInTheDocument();
    expect(within(strip()).getByText(/−6,000 from the previous request/)).toBeInTheDocument();
    expect(within(strip()).queryByText(/after a compaction/)).not.toBeInTheDocument();
    fireEvent.keyDown(trendGroup(), { key: 'ArrowLeft' });
    expect(within(strip()).getByText('Request 3 · turn 2 · 8,010')).toBeInTheDocument();
    expect(within(strip()).getByText(/−52K from the previous request/)).toBeInTheDocument();
    expect(within(strip()).getByText(/after a compaction/)).toBeInTheDocument();
    // Delta is the signed change, on the same chart: there is no list of changes.
    fireEvent.click(within(trend).getByRole('button', { name: 'Delta' }));
    expect(within(trend).getByRole('button', { name: 'Delta' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(trend).getByText(/turn 2 · observed −52K/)).toBeInTheDocument();
  });

  it('lists the compaction once in the events and selects its request on the trend', async () => {
    const { onOpenPoint } = show();
    const events = await vi.waitFor(() => card('Context events'));
    const filters = within(events).getByRole('group', { name: 'Show events of one kind' });
    // An older recording: the compaction is in the view's record, not in its events.
    expect(within(filters).getByRole('button', { name: /Compactions 1/ })).toBeInTheDocument();
    const rows = within(events).getAllByRole('listitem');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('Trigger: Automatic');
    expect(rows[0]).toHaveTextContent('The CLI reported 60K tokens before');
    expect(rows[0]).toHaveTextContent('Requests observed around it: 60K → 8,010');

    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Show in trend' }));
    expect(within(strip()).getByText('Request 3 · turn 2 · 8,010')).toBeInTheDocument();
    expect(
      await within(card('Context Browser')).findByText('Request 3 · turn 2'),
    ).toBeInTheDocument();
    expect(card('Selected request')).toBeInTheDocument();
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 777 });
  });

  it('joins a compaction event to the record of it, listing it once', async () => {
    const data = view();
    data.events = [
      {
        kind: 'compaction',
        source: 'cli_compact_boundary',
        label: 'manual',
        names: [],
        items: null,
        job_id: 'j2',
        turn_no: 2,
        attempt_id: 2,
        seq: 1,
        event_id: 777,
        at: '2026-10-15T11:30:00Z',
        chars: null,
        tokens: null,
        tokens_basis: null,
        before_tokens: 60_000,
        after_tokens: null,
        from_value: null,
        to_value: null,
        superseded: false,
        copied: false,
      },
    ];
    data.event_counts = { compaction: 1 };
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    show();
    const events = await vi.waitFor(() => card('Context events'));
    const rows = within(events).getAllByRole('listitem');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('Trigger: Manual');
    expect(rows[0]).toHaveTextContent('Requests observed around it: 60K → 8,010');
  });

  it('links a compaction to its turn column when its request is not the turn’s last', async () => {
    const data = view();
    // 130 turns of two requests each: request 5 is turn 3's first, and the
    // default window of 120 turn columns would not show turn 3.
    data.requests = Array.from({ length: 260 }, (_, i) =>
      point(i, 1_000 + i * 10, {
        job_id: `t${1 + Math.floor(i / 2)}`,
        turn_no: 1 + Math.floor(i / 2),
        attempt_id: 1 + Math.floor(i / 2),
        seq: i + 1,
        event_id: 500 + i,
      }),
    );
    data.latest = { ...data.latest!, point: data.requests[259] };
    data.peak = data.requests[259];
    data.events = [];
    data.event_counts = { compaction: 1 };
    data.compactions = [
      {
        job_id: 't3',
        turn_no: 3,
        attempt_id: 3,
        seq: 4,
        event_id: 900,
        at: '2026-10-15T11:30:00Z',
        source: 'cli_compact_boundary',
        trigger: 'auto',
        pre_tokens: 90_000,
        post_tokens: 9_000,
        completed: true,
        observed_before: 1_040,
        observed_after: 1_040,
      },
    ];
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    // The browser's composition is not the subject here; its reply is absent.
    vi.mocked(getRequestComposition).mockResolvedValue(null);
    show();
    const trend = await vi.waitFor(() => card('Context Trend'));
    fireEvent.click(within(trend).getByRole('button', { name: 'Turn' }));
    expect(within(trend).getByText('Showing 11–130 of 130')).toBeInTheDocument();
    const events = card('Context events');
    fireEvent.click(await within(events).findByRole('button', { name: 'Show in trend' }));
    // The containing turn column comes into view and is highlighted; the strip
    // and browser keep the exact request the compaction selects (request 5).
    expect(within(trend).getByText('Showing 3–122 of 130')).toBeInTheDocument();
    expect(within(trend).getByText(/Turn 3 · 2 requests/)).toBeInTheDocument();
    const strip = within(trend).getByRole('group', { name: 'Request details' });
    expect(within(strip).getByText('Request 5 · turn 3 · 1,040')).toBeInTheDocument();
    expect(
      await within(card('Context Browser')).findByText('Request 5 · turn 3'),
    ).toBeInTheDocument();
    // Arrow keys move turn columns from the containing turn, not from the end.
    fireEvent.keyDown(within(trend).getByRole('group', { name: /Context of 130 columns/ }), {
      key: 'ArrowRight',
    });
    expect(within(strip).getByText('Request 8 · turn 4 · 1,070')).toBeInTheDocument();
  });

  it('opens the selected request’s step and turn from the strip under the trend', async () => {
    const { onOpenPoint, onOpenTurn } = show();
    await screen.findByText('Context Browser');
    const trend = card('Context Trend');
    expect(trend).toContainElement(strip());
    fireEvent.click(within(strip()).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 503 });
    fireEvent.keyDown(trendGroup(), { key: 'ArrowLeft' });
    fireEvent.keyDown(trendGroup(), { key: 'ArrowLeft' });
    fireEvent.click(within(strip()).getByRole('button', { name: 'Open turn' }));
    expect(onOpenTurn).toHaveBeenLastCalledWith('j1', 1);
  });

  it('has no table of what each agent reports, nor the notes that sat under it', async () => {
    // Every count that once added a note under that table is non-zero here.
    vi.mocked(getAgentThreadContext).mockResolvedValue(
      view({
        coverage: {
          ...view().coverage,
          attempts_observed: 1,
          points_omitted: 4,
          unparsed_raw: 2,
          compactions_unknown_outcome: 1,
        },
      }),
    );
    show();
    expect(await screen.findByText('Context Browser')).toBeInTheDocument();
    expect(within(await fact('Live occupancy')).getByText('Not collected')).toBeInTheDocument();
    for (const text of [
      /What each agent reports/,
      /runs carry observations/,
      /earlier requests are not drawn/,
      /stored records were clipped too short/,
      /compactions have no recorded outcome/,
      /Window reported for \d+ of \d+ turns/,
      /Read for \d+ of \d+ turns/,
      'Tool and MCP names, no sizes',
      'Reported from this release on',
      'Per-request input and output',
    ]) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
  });

  it('reads as one flow in Chinese too', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    show();
    expect(await screen.findByRole('heading', { name: '上下文浏览器' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '上下文趋势' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '上下文事件' })).toBeInTheDocument();
    expect(within(await fact('实时占用')).getByText('未采集')).toBeInTheDocument();
    expect(within(await fact('模型请求')).getByText(/^4 · /)).toBeInTheDocument();
    expect(screen.getByRole('group', { name: '请求详情' })).toBeInTheDocument();
    for (const name of ['各轮', '压缩记录', '变化最大的请求', '请求大小（报告值）']) {
      expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('heading', { name: '各 Agent 报告的内容' })).not.toBeInTheDocument();
    expect(screen.queryByText(/带有观测数据/)).not.toBeInTheDocument();
  });

  it('is honestly empty for a conversation without request sizes', async () => {
    vi.mocked(getAgentThreadContext).mockResolvedValue(
      view({
        requests: [],
        latest: null,
        peak: null,
        largest_changes: [],
        compactions: [],
        coverage: { ...view().coverage, attempts_observed: 0, main_requests: 0, compactions: 0 },
      }),
    );
    show();
    expect(
      (await screen.findAllByText('No model request sizes are recorded for this conversation.'))
        .length,
    ).toBeGreaterThan(0);
    // No request: no donut, no strip, no window share.
    expect(screen.queryByText(/%/, { selector: 'b' })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Request details' })).not.toBeInTheDocument();
    // Nothing observed: the counts are unknown, not zero.
    expect(within(await fact('Model requests')).getByText('—')).toBeInTheDocument();
    expect(within(await fact('Largest request')).getByText('—')).toBeInTheDocument();
    expect(
      screen.getByText('No context events are recorded for this conversation.'),
    ).toBeInTheDocument();
  });

  it('says so when the conversation is not the reader’s', async () => {
    vi.mocked(getAgentThreadContext).mockResolvedValue(null);
    show();
    expect(
      await screen.findByText('This conversation has no context to show.'),
    ).toBeInTheDocument();
  });

  it('reads the composition of the selected request, the newest by default', async () => {
    show();
    expect(await screen.findByText('Context Browser')).toBeInTheDocument();
    expect(await screen.findByText('Request 4 · turn 2')).toBeInTheDocument();
    // The newest request, and the one before it for the change by item.
    expect(getRequestComposition).toHaveBeenCalledWith('j2', 503, expect.anything());
    expect(getRequestComposition).toHaveBeenCalledWith('j2', 502, expect.anything());
    for (let i = 0; i < 3; i += 1) fireEvent.keyDown(trendGroup(), { key: 'ArrowLeft' });
    await vi.waitFor(() =>
      expect(getRequestComposition).toHaveBeenCalledWith('j2', 500, expect.anything()),
    );
  });

  it('keeps provider input, the CLI account and the reconstruction apart', async () => {
    show();
    expect(await screen.findByText(/Reported input: 2,010/)).toBeInTheDocument();
    expect(screen.getByText(/not more content/)).toBeInTheDocument();
    // Claude's start-up names: counted as names, never sized, never live.
    expect(
      screen.getByText(
        /^Named by Claude Code when turn \d+ started\..*the platform does not ask\.$/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('MCP servers: github (connected)')).toBeInTheDocument();
    expect(screen.getByText('2 named')).toBeInTheDocument();
    // Named by the CLI as not exposed, and listed by the browser as not recorded.
    expect(screen.getAllByText('System prompt')).toHaveLength(2);
    expect(screen.getByText('Not exposed by this CLI')).toBeInTheDocument();
    expect(screen.queryByText(/in context, per the CLI/)).not.toBeInTheDocument();
    expect(screen.getByText(/It is not the assembled request/)).toBeInTheDocument();
    expect(
      screen.getByText(
        /Starts after the compaction in turn 2\. The CLI can keep some recent messages/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Before the compaction: 6')).toBeInTheDocument();
    expect(screen.getByText('In subagents’ own contexts: 1')).toBeInTheDocument();
    // A clipped item is flagged on its category's row before it is opened.
    expect(screen.getByRole('button', { name: /Tool results/ })).toHaveTextContent('1 clipped');
  });

  it('expands an item into its stored text and opens its step or turn', async () => {
    vi.mocked(getEventContent).mockResolvedValue({
      event_id: 777,
      event_type: 'tool_result',
      phase: null,
      offset: 0,
      text: '中文 output',
      total_chars: 9,
      truncated: true,
      next_offset: null,
    });
    const { onOpenPoint, onOpenTurn } = show();
    fireEvent.click(await screen.findByRole('button', { name: /Tool results/, expanded: false }));
    fireEvent.click(await screen.findByRole('button', { name: 'Show content' }));
    expect(await screen.findByText('中文 output')).toBeInTheDocument();
    expect(getEventContent).toHaveBeenCalledWith('j2', 777, 0);
    expect(screen.getByText(/This is the clipped preview the platform stored/)).toBeInTheDocument();
    const steps = screen.getAllByRole('button', { name: 'Open step' });
    fireEvent.click(steps[steps.length - 1]);
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 777 });
    const turns = screen.getAllByRole('button', { name: 'Open turn' });
    fireEvent.click(turns[turns.length - 1]);
    expect(onOpenTurn).toHaveBeenLastCalledWith('j2', 2);
  });

  it('says why there is no CLI account, without inventing one', async () => {
    vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
      composition(eventId, { native: null, native_relation: null, native_status: 'not_collected' }),
    );
    show();
    expect(
      await screen.findByText(/the read did not finish while the turn ran/),
    ).toBeInTheDocument();
    for (const [status, text] of [
      ['not_settled', /retried, failed or was cancelled before the read arrived/],
      ['pending', /read as the turn starts; the read has not arrived yet/],
    ] as const) {
      vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
        composition(eventId, { native: null, native_relation: null, native_status: status }),
      );
      cleanup();
      show();
      expect(await screen.findByText(text)).toBeInTheDocument();
    }
    vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
      composition(eventId, {
        runtime: 'codex',
        native: null,
        native_relation: null,
        native_status: 'not_offered',
      }),
    );
    cleanup();
    show();
    expect(
      await screen.findByText(/codex offers no supported way to read its context/),
    ).toBeInTheDocument();
    vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
      composition(eventId, {
        runtime: 'pi',
        native: null,
        native_relation: null,
        native_status: 'not_offered',
      }),
    );
    cleanup();
    show();
    expect(
      await screen.findByText(/so the platform does not ask and a turn never waits for it/),
    ).toBeInTheDocument();
  });

  it('says when a read at the turn start came before this request', async () => {
    const read = (relation: RequestCompositionApi['native_relation']) =>
      vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
        composition(eventId, {
          runtime: 'opencode',
          native: {
            v: 2,
            source: 'opencode_session_messages',
            status: 'ok',
            token_basis: 'chars_cjk_v1',
            observed: 'turn_start',
            total_tokens: null,
            categories: [
              { key: 'user_messages', tokens: 13, entries: 1, exposed: true },
              { key: 'system_prompt', tokens: null, exposed: false },
            ],
            entries: [],
          },
          native_relation: relation,
        }),
      );
    read({ event_id: 901, requests_before: 0, compacted_since: false });
    show();
    expect(
      await screen.findByText(
        /^Read when turn \d+ started: what this request was sent with, as the CLI’s server listed it\. Sizes estimated from the text the CLI listed\.$/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Not exposed by this CLI')).toBeInTheDocument();
    read({ event_id: 901, requests_before: 2, compacted_since: true });
    cleanup();
    show();
    expect(
      await screen.findByText(
        /before this request\. Requests of this turn since then: 2; what they added is not in it\. A compaction later in this turn replaced part of what is listed here\./,
      ),
    ).toBeInTheDocument();
  });

  it('filters the reconstruction by category', async () => {
    show();
    const row = await screen.findByRole('button', { name: /Tool results/, expanded: false });
    expect(row).toHaveTextContent('≈1,000');
    expect(screen.queryByText('line line line')).not.toBeInTheDocument();
    fireEvent.click(row);
    expect(screen.queryByText('继续 continue')).not.toBeInTheDocument();
    expect(screen.getByText('line line line')).toBeInTheDocument();
    // System prompt and tool schemas are never recorded: said so, never 0.
    expect(screen.getAllByText('not recorded').length).toBeGreaterThan(0);
  });
});

describe("File Activity follows the Context view's settlement", () => {
  function fileView(status: string): FileActivityApi {
    return {
      thread_id: 't',
      generated_at: '2026-10-31T09:00:00Z',
      sort: 'active',
      filter: 'all',
      q: '',
      totals: {
        files: 1,
        operations: 1,
        reads: 0,
        writes: status === 'succeeded' ? 1 : 0,
        searches: 0,
        images: 0,
        added: 0,
        removed: 0,
        added_unknown_files: 0,
        removed_unknown_files: 0,
        failed: status === 'failed' ? 1 : 0,
        pending: status === 'pending' ? 1 : 0,
        not_recorded: status === 'not_recorded' ? 1 : 0,
        unknown_path_operations: 0,
        search_unknown_scope: 0,
        subagent_operations: 0,
      },
      filter_counts: { all: 1, read: 0, written: 1, searched: 0, images: 0 },
      coverage: {
        turns: 1,
        attempts: 1,
        events_scanned: 2,
        events_capped: false,
        window_first_at: null,
        window_last_at: null,
      },
      semantics: {
        line_changes: 'cumulative_recorded',
        net_git_diff: 'not_read',
        search_scope: 'explicit_path_only',
        image_evidence: 'explicit_only',
        counts: 'successful_operations',
      },
      files: [
        {
          path: '/workspace/a.ts',
          name: 'a.ts',
          type_label: 'TS',
          file_kind: 'code',
          reads: 0,
          writes: status === 'succeeded' ? 1 : 0,
          searches: 0,
          images: 0,
          failed: status === 'failed' ? 1 : 0,
          pending: status === 'pending' ? 1 : 0,
          not_recorded: status === 'not_recorded' ? 1 : 0,
          added: 0,
          removed: 0,
          added_unknown_ops: 0,
          removed_unknown_ops: 0,
          operations: 1,
          events: 2,
          subagent_operations: 0,
          attempts: 1,
          first_at: null,
          last_at: '2026-10-31T09:00:00Z',
        },
      ],
      has_more: false,
      next_cursor: null,
    };
  }

  function contextWithState(state: string) {
    const base = view();
    return {
      ...base,
      turns: base.turns.map((item, index) =>
        index === base.turns.length - 1 ? { ...item, state } : item,
      ),
    };
  }

  it.each([
    ['failed', 'not_recorded'],
    ['cancelled', 'not_recorded'],
    ['succeeded', 'succeeded'],
  ])(
    'notices a turn becoming %s with the same turns and requests',
    async (state, expectedStatus) => {
      vi.mocked(getAgentThreadContext)
        .mockResolvedValueOnce(contextWithState('running'))
        .mockResolvedValue(contextWithState(state));
      vi.mocked(getAgentFileActivity).mockReset();
      vi.mocked(getAgentFileActivity)
        .mockResolvedValueOnce(fileView('pending'))
        .mockResolvedValue(fileView(expectedStatus));
      // The page mounts while the turn runs, then the same turn settles: the
      // job state is what changes, and the provider re-reads on it.
      const runningJob = {
        ...JOB,
        state: 'running',
        conversationState: 'running',
      } as unknown as AgentJob;
      const settledJob = { ...JOB, state, conversationState: 'idle' } as unknown as AgentJob;
      const { rerender } = render(
        <LocaleProvider>
          <ContextView job={runningJob} onOpenPoint={vi.fn()} onOpenTurn={vi.fn()} />
        </LocaleProvider>,
      );
      await screen.findByText('File Activity');
      await waitFor(() => expect(vi.mocked(getAgentFileActivity).mock.calls.length).toBe(1));
      rerender(
        <LocaleProvider>
          <ContextView job={settledJob} onOpenPoint={vi.fn()} onOpenTurn={vi.fn()} />
        </LocaleProvider>,
      );
      await waitFor(() =>
        expect(vi.mocked(getAgentFileActivity).mock.calls.length).toBeGreaterThan(1),
      );
      expect(contextWithState(state).turns.length).toBe(contextWithState('running').turns.length);
      expect(contextWithState(state).requests.length).toBe(
        contextWithState('running').requests.length,
      );
    },
  );
});
