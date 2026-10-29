// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentThreadContext,
  type ContextPointApi,
  type ThreadContextApi,
} from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import type { AgentJob } from '../types';
import { ContextView } from './ContextView';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentThreadContext: vi.fn(),
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
    composition: {
      basis: 'since_last_compaction',
      chars_per_token: 4,
      categories: { user: 0, assistant: 210, reasoning: 0, tool_calls: 0, tool_results: 0 },
      estimated_tokens: 210,
      truncated: true,
      unattributed_tokens: 1_800,
    },
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

beforeEach(() => {
  vi.mocked(getAgentThreadContext).mockResolvedValue(view());
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
    expect(await screen.findByText(/compacted the context after this request/)).toBeInTheDocument();
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

  it('labels the composition an estimate and keeps what it cannot attribute apart', async () => {
    show();
    expect(
      await screen.findByText(/Text stored since the last compaction, at 4 characters per token/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Some stored previews were shortened/)).toBeInTheDocument();
    expect(screen.getByText('Not attributable')).toBeInTheDocument();
  });

  it('says what each runtime reports, and how much of it this conversation has', async () => {
    show();
    const table = (await screen.findByRole('heading', { name: 'What each agent reports' }))
      .parentElement!;
    expect(within(table).getByText('Compaction sizes').closest('tr')).toHaveTextContent(
      'Reported from this release on',
    );
    expect(within(table).getByText('Window reported for 0 of 2 turns')).toBeInTheDocument();
    expect(within(table).getByText('Live occupancy').closest('tr')).toHaveTextContent(
      'Not collected',
    );
  });

  it('is honestly empty for a conversation without request sizes', async () => {
    vi.mocked(getAgentThreadContext).mockResolvedValue(
      view({
        requests: [],
        latest: null,
        peak: null,
        largest_changes: [],
        compactions: [],
        composition: null,
      }),
    );
    show();
    expect(
      await screen.findByText('No model request sizes are recorded for this conversation.'),
    ).toBeInTheDocument();
    expect(within(await fact('Latest observed context')).getByText('—')).toBeInTheDocument();
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
});
