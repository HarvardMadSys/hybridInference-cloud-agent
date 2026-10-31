// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentFileActivity,
  getAgentFileOperations,
  getAgentThreadContext,
  getEventContent,
  getRequestComposition,
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

describe('ContextView', () => {
  it('names the latest request as observed, never as the live window', async () => {
    show();
    const latest = await fact('Latest observed context');
    expect(within(latest).getByText('2,010')).toBeInTheDocument();
    expect(within(await fact('Live occupancy')).getByText('Not collected')).toBeInTheDocument();
    // No window reported: no share, and no 0%.
    const window = await fact('Context window');
    expect(within(window).getByText('—')).toBeInTheDocument();
    expect(within(window).getByText('Not reported for this request')).toBeInTheDocument();
    expect(screen.queryByText(/%/, { selector: 'dd' })).not.toBeInTheDocument();
    expect(
      within(await fact('Model requests')).getByText('3 more by subagents, in their own contexts'),
    ).toBeInTheDocument();
  });

  it('shows a reported window as a share of it', async () => {
    const data = view();
    data.latest = {
      ...data.latest!,
      window_share: 0.25,
      point: { ...data.latest!.point, context_window: 200_000 },
    };
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    show();
    expect(within(await fact('Context window')).getByText('25% of 200K')).toBeInTheDocument();
  });

  it('warns when a compaction came after the latest observation', async () => {
    const data = view();
    data.latest = { ...data.latest!, compacted_after: true };
    vi.mocked(getAgentThreadContext).mockResolvedValue(data);
    show();
    // On the window card and in the summary, both about the observed request.
    expect(await screen.findAllByText(/compacted the context after this request/)).toHaveLength(2);
  });

  it('marks only reported compactions, and calls a later drop a drop', async () => {
    show();
    const changes = await screen.findByRole('heading', { name: 'Largest changes' });
    const list = changes.parentElement!;
    expect(within(list).getByText(/request 3, turn 2 · after a compaction/)).toBeInTheDocument();
    expect(
      within(list).getByText(/request 4, turn 2 · no compaction reported/),
    ).toBeInTheDocument();
    const svg = screen.getByRole('img', { name: 'Input tokens of each model request' });
    expect(svg.querySelectorAll('line title')).toHaveLength(1);
  });

  it('opens the step a request, a change or a compaction names, and its turn', async () => {
    const { onOpenPoint, onOpenTurn } = show();
    fireEvent.click(await screen.findByRole('button', { name: /−52K/ }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Open step' })[0]);
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 502 });
    const compactions = screen.getByRole('heading', { name: 'Compactions' }).parentElement!;
    fireEvent.click(within(compactions).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 777 });
    fireEvent.click(screen.getByRole('button', { name: '#1' }));
    expect(onOpenTurn).toHaveBeenLastCalledWith('j1', 1);
  });

  it('moves along the requests with the keyboard', async () => {
    show();
    const svg = await screen.findByRole('img', { name: 'Input tokens of each model request' });
    fireEvent.keyDown(svg, { key: 'Home' });
    expect(screen.getAllByText('Request 1 · turn 1 · 1,010').length).toBeGreaterThan(0);
    fireEvent.keyDown(svg, { key: 'ArrowRight' });
    expect(screen.getAllByText('Request 2 · turn 1 · 60K').length).toBeGreaterThan(0);
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
    // The real content is all there: the facts, the request chart, the turns.
    expect(within(await fact('Latest observed context')).getByText('2,010')).toBeInTheDocument();
    expect(within(await fact('Live occupancy')).getByText('Not collected')).toBeInTheDocument();
    expect(
      within(await fact('Context window')).getByText('Not reported for this request'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Request sizes (as reported)' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Turns' })).toBeInTheDocument();
    expect(
      screen.getByRole('img', { name: 'Input tokens of each model request' }),
    ).toBeInTheDocument();
    // The section and its diagnostics are gone, not collapsed.
    expect(
      screen.queryByRole('heading', { name: 'What each agent reports' }),
    ).not.toBeInTheDocument();
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

  it('has no such table in Chinese either', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    show();
    expect(within(await fact('最近观测到的上下文')).getByText('2,010')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '各轮' })).toBeInTheDocument();
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
    expect(within(await fact('Latest observed context')).getByText('—')).toBeInTheDocument();
    // Nothing observed: the counts are unknown, not zero.
    expect(within(await fact('Model requests')).getByText('—')).toBeInTheDocument();
    expect(within(await fact('Compactions')).getByText('—')).toBeInTheDocument();
  });

  it('says so when the conversation is not the reader’s', async () => {
    vi.mocked(getAgentThreadContext).mockResolvedValue(null);
    show();
    expect(
      await screen.findByText('This conversation has no context to show.'),
    ).toBeInTheDocument();
  });

  it('answers in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    show();
    expect(await screen.findByText('最近观测到的上下文')).toBeInTheDocument();
    expect(within(await fact('实时占用')).getByText('未采集')).toBeInTheDocument();
  });

  it('reads the composition of the selected request, the newest by default', async () => {
    show();
    expect(await screen.findByText('Context Browser')).toBeInTheDocument();
    expect(await screen.findByText('Request 4 · turn 2')).toBeInTheDocument();
    // The newest request, and the one before it for the change by item.
    expect(getRequestComposition).toHaveBeenCalledWith('j2', 503, expect.anything());
    expect(getRequestComposition).toHaveBeenCalledWith('j2', 502, expect.anything());
    const svg = screen.getByRole('img', { name: 'Input tokens of each model request' });
    fireEvent.keyDown(svg, { key: 'Home' });
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
