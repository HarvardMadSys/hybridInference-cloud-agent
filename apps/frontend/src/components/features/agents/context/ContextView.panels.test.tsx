// @vitest-environment jsdom
// The two compact cards the layout cleanup had to keep, in the merged page:
// Command Executions from #1072 and File Activity from #1073 below the events,
// with the page's navigation callbacks and the running-turn refresh intact.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getAgentFileActivity,
  getAgentFileOperations,
  getAgentThreadContext,
  getRequestComposition,
  getThreadContextCommands,
  getThreadContextDna,
  getThreadContextTiming,
  type ContextPointApi,
  type FileActivityApi,
  type FileOperationsPageApi,
  type ThreadCommandsApi,
  type ThreadContextApi,
} from '@/lib/api/agents';
import type { AgentJob } from '../types';
import { ContextView } from './ContextView';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getAgentThreadContext: vi.fn(),
  getRequestComposition: vi.fn().mockResolvedValue(null),
  getEventContent: vi.fn(),
  getThreadContextTiming: vi.fn().mockResolvedValue(null),
  getThreadContextDna: vi.fn().mockResolvedValue(null),
  getThreadContextCommands: vi.fn(),
  getAgentFileActivity: vi.fn(),
  getAgentFileOperations: vi.fn(),
}));

function point(index: number, overrides: Partial<ContextPointApi> = {}): ContextPointApi {
  return {
    index,
    job_id: index < 2 ? 'j1' : 'j2',
    turn_no: index < 2 ? 1 : 2,
    attempt_id: index < 2 ? 1 : 2,
    seq: index + 1,
    event_id: 500 + index,
    at: '2026-10-15T11:00:00Z',
    prompt_tokens: 1_010 + index,
    cache_read_tokens: 1_000,
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

function view(): ThreadContextApi {
  const requests = [point(0), point(1), point(2), point(3)];
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
        last_prompt_tokens: 1_011,
        peak_prompt_tokens: 1_011,
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
        first_prompt_tokens: 1_012,
        last_prompt_tokens: 1_013,
        peak_prompt_tokens: 1_013,
        change_tokens: 1,
        compactions: 0,
        context_window: null,
        first_index: 2,
      },
    ],
    requests,
    compactions: [],
    largest_changes: [],
    coverage: {
      turns: 2,
      attempts: 2,
      attempts_observed: 2,
      main_requests: 4,
      subagent_requests: 0,
      compactions: 0,
      compactions_unknown_outcome: 0,
      points_omitted: 0,
      unparsed_raw: 0,
    },
    events: [],
    event_counts: {},
    events_omitted: 0,
    event_capabilities: [],
  };
}

function commandsPage(): ThreadCommandsApi {
  const base = {
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    attempt_no: 1,
    runtime: 'claude-code',
    tool: 'Bash',
    command_chars: 9,
    command_truncated: false,
    cwd: '/work',
    tool_error: false,
    background: false,
    superseded: false,
    duration_ms: 1_200,
    duration_basis: 'recorded',
    at: '2026-10-31T09:00:00Z',
    result_at: '2026-10-31T09:00:01Z',
    output_preview: 'ok',
    output_chars: 2,
    output_truncated: false,
    parse_basis: 'executable',
  } as const;
  return {
    thread_id: 't',
    generated_at: '2026-10-31T09:00:00Z',
    slow_cutoff_ms: 10_000,
    summary: {
      total: 2,
      settled: 2,
      failed: 1,
      failure_rate: 0.5,
      running: 0,
      unknown: 0,
      cumulative_ms: 3_000,
      durations_known: 2,
      durations_missing: 0,
      duration_basis: 'recorded',
      background: 0,
    },
    coverage: {
      executions_total: 2,
      executions_covered: 2,
      truncated: false,
      turns: 2,
      attempts: 2,
      copied_jobs_excluded: 0,
      unsupported_runtimes: [],
      results_partial: false,
    },
    counts: { all: 2, failed: 1, slow: 0, background: 0 },
    attention: { slowest: null, failing: null, repeats: null, background: 0 },
    programs: [],
    executions: [
      {
        ...base,
        id: '1:c1',
        command: 'pytest -q',
        status: 'completed',
        exit_code: 0,
        event_id: 601,
        result_event_id: 602,
        program: 'pytest',
      },
      {
        ...base,
        id: '1:c2',
        job_id: 'j2',
        turn_no: 2,
        attempt_id: 2,
        command: 'make build',
        command_chars: 10,
        status: 'failed',
        exit_code: 2,
        tool_error: true,
        duration_ms: 1_800,
        at: '2026-10-31T09:05:00Z',
        result_at: '2026-10-31T09:05:02Z',
        output_preview: 'boom',
        output_chars: 4,
        event_id: 603,
        result_event_id: 604,
        program: 'make',
      },
    ],
    page: { total: 2, returned: 2, limit: 100, next_cursor: null },
    filter: 'all',
    sort: 'latest',
    q: '',
    program: null,
  };
}

function filesPage(): FileActivityApi {
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
      writes: 1,
      searches: 0,
      images: 0,
      added: 4,
      removed: 1,
      added_unknown_files: 0,
      removed_unknown_files: 0,
      failed: 0,
      pending: 0,
      not_recorded: 0,
      unknown_path_operations: 0,
      search_unknown_scope: 0,
      subagent_operations: 0,
    },
    filter_counts: { all: 1, read: 0, written: 1, searched: 0, images: 0 },
    coverage: {
      turns: 2,
      attempts: 2,
      events_scanned: 12,
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
        writes: 1,
        searches: 0,
        images: 0,
        failed: 0,
        pending: 0,
        not_recorded: 0,
        added: 4,
        removed: 1,
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

function operationsPage(): FileOperationsPageApi {
  return {
    thread_id: 't',
    path: '/workspace/a.ts',
    total: 1,
    operations: [
      {
        call_id: 'call-1',
        event_id: 9,
        result_event_id: 10,
        attempt_id: 2,
        attempt_no: 1,
        job_id: 'j2',
        turn_no: 2,
        seq: 9,
        at: '2026-10-31T09:00:00Z',
        tool: 'Write',
        action: 'write',
        kind: 'write',
        status: 'succeeded',
        path: '/workspace/a.ts',
        name: 'a.ts',
        path_basis: 'cwd_relative',
        added: 4,
        removed: 1,
        patch_index: null,
        patch_files: null,
        renamed_to: null,
        subagent: false,
        superseded: false,
        clipped: false,
      },
    ],
    has_earlier: false,
    next_cursor: null,
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

function card(title: string): HTMLElement {
  return screen.getByRole('heading', { name: title }).closest('section')!;
}

beforeEach(() => {
  vi.mocked(getAgentThreadContext).mockResolvedValue(view());
  vi.mocked(getRequestComposition).mockResolvedValue(null);
  vi.mocked(getThreadContextTiming).mockResolvedValue(null);
  vi.mocked(getThreadContextDna).mockResolvedValue(null);
  vi.mocked(getThreadContextCommands).mockResolvedValue(commandsPage());
  vi.mocked(getAgentFileActivity).mockResolvedValue(filesPage());
  vi.mocked(getAgentFileOperations).mockResolvedValue(operationsPage());
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe('Command Executions and File Activity in the one-flow layout', () => {
  it('keeps both compact cards below the events, one of each', async () => {
    show();
    const events = await vi.waitFor(() => card('Context events'));
    const files = await vi.waitFor(() => card('File Activity'));
    const commands = await vi.waitFor(() => card('Command Executions'));
    expect(events.compareDocumentPosition(files)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(files.compareDocumentPosition(commands)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // No legacy block came back with them.
    for (const name of ['Compactions', 'Largest changes', 'Turns', 'What each agent reports']) {
      expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Command Executions' })).toHaveLength(1);
    expect(screen.getAllByRole('heading', { name: 'File Activity' })).toHaveLength(1);
  });

  it('opens a command and a file operation with the page callbacks', async () => {
    const { onOpenPoint, onOpenTurn } = show();
    const commands = await vi.waitFor(() => card('Command Executions'));
    const list = within(commands).getByRole('list', { name: 'Command Executions' });
    const first = within(list).getAllByRole('listitem')[0];
    fireEvent.click(within(first).getByRole('button', { expanded: false }));
    const details = within(first).getByLabelText('Execution details');
    fireEvent.click(within(details).getByRole('button', { name: 'Open step' }));
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j1', turnNo: 1, eventId: 601 });
    fireEvent.click(within(details).getByRole('button', { name: 'Open turn' }));
    expect(onOpenTurn).toHaveBeenLastCalledWith('j1', 1);

    const files = await vi.waitFor(() => card('File Activity'));
    fireEvent.click(within(files).getByLabelText('Show operations on a.ts'));
    expect(await within(files).findByText(/of 1 recorded operations shown/)).toBeInTheDocument();
    fireEvent.click(within(files).getAllByRole('button', { name: 'Step' })[0]);
    expect(onOpenPoint).toHaveBeenLastCalledWith({ jobId: 'j2', turnNo: 2, eventId: 10 });
    fireEvent.click(within(files).getAllByRole('button', { name: 'Turn' })[0]);
    expect(onOpenTurn).toHaveBeenLastCalledWith('j2', 2);
  });

  it('re-reads both cards when the running turn settles', async () => {
    const running = view();
    running.latest = { ...running.latest!, turn_active: true };
    running.turns = running.turns.map((turn, index) =>
      index === running.turns.length - 1 ? { ...turn, state: 'running' } : turn,
    );
    vi.mocked(getAgentThreadContext).mockResolvedValue(running);

    const runningJob = {
      ...JOB,
      state: 'running',
      conversationState: 'running',
    } as unknown as AgentJob;
    const settledJob = {
      ...JOB,
      state: 'succeeded',
      conversationState: 'idle',
    } as unknown as AgentJob;
    const { rerender } = render(
      <LocaleProvider>
        <ContextView job={runningJob} onOpenPoint={vi.fn()} onOpenTurn={vi.fn()} />
      </LocaleProvider>,
    );
    await screen.findByText('File Activity');
    await screen.findByRole('heading', { name: 'Command Executions' });
    const commandsBefore = vi.mocked(getThreadContextCommands).mock.calls.length;
    const filesBefore = vi.mocked(getAgentFileActivity).mock.calls.length;

    vi.mocked(getAgentThreadContext).mockResolvedValue(view());
    rerender(
      <LocaleProvider>
        <ContextView job={settledJob} onOpenPoint={vi.fn()} onOpenTurn={vi.fn()} />
      </LocaleProvider>,
    );
    await waitFor(() =>
      expect(vi.mocked(getThreadContextCommands).mock.calls.length).toBeGreaterThan(commandsBefore),
    );
    await waitFor(() =>
      expect(vi.mocked(getAgentFileActivity).mock.calls.length).toBeGreaterThan(filesBefore),
    );
  });
});
