// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
// No user-event dependency in this repo; fireEvent is the house click.
import { afterEach, describe, expect, it } from 'vitest';

import { toDisplayJob, toTurnResources } from './adapt';
import { TurnFooter, formatBytes, formatCpu, formatDuration, toolTimings } from './TurnFooter';
import type { AgentEvent, AgentJob, TurnResources } from './types';

afterEach(cleanup);

const RESOURCES: TurnResources = {
  cpuUsec: 18_200_000,
  memPeakBytes: 1_288_490_188,
  memAvgBytes: 600_000_000,
  ioReadBytes: 1024,
  ioWriteBytes: 2048,
  netRxBytes: 35_651_584,
  netTxBytes: 2_097_152,
  sampleCount: 42,
  samplerVersion: '2',
  coverage: 1,
  toolTimeMs: null,
  toolCalls: [],
  toolCallsTruncated: false,
};

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'a turn',
    state: 'done',
    repo: 'o/r',
    baseSha: 'abc1234',
    branch: 'agent/t',
    runtime: 'claude-code',
    model: 'glm-4.6',
    spentUsd: 0,
    timeoutLabel: '',
    sandbox: '',
    attempts: [],
    events: [],
    diffFiles: [],
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 0 },
    ...overrides,
  };
}

describe('formatters', () => {
  it('reads durations the way a person would say them', () => {
    expect(formatDuration(420)).toBe('420ms');
    expect(formatDuration(4_200)).toBe('4.2s');
    expect(formatDuration(42_000)).toBe('42s');
    expect(formatDuration(125_000)).toBe('2m 5s');
  });

  it('refuses to render a number it was not given', () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
    expect(formatBytes(null)).toBeNull();
    expect(formatCpu(null)).toBeNull();
  });

  it('scales bytes and cpu time to a readable unit', () => {
    expect(formatBytes(1024)).toBe('1KB');
    expect(formatBytes(1_288_490_188)).toBe('1.2GB');
    expect(formatCpu(18_200_000)).toBe('18.2s');
  });

  it('carries rounding across the unit boundary rather than printing past it', () => {
    // Just under a gigabyte, which rounds to 1024MB before it climbs.
    expect(formatBytes(1_073_741_823)).toBe('1GB');
    // Just under a minute, which rounds to 60 seconds before it becomes one.
    expect(formatDuration(59_960)).toBe('1m');
    expect(formatDuration(119_600)).toBe('2m');
  });
});

describe('toolTimings', () => {
  const events: AgentEvent[] = [
    {
      kind: 'tool_use',
      tool: 'Bash',
      detail: 'cargo build',
      id: 't1',
      at: '2026-08-19T10:00:00.000Z',
    },
    {
      kind: 'tool_result',
      text: 'ok',
      toolUseId: 't1',
      isError: false,
      at: '2026-08-19T10:00:15.000Z',
    },
    {
      kind: 'tool_use',
      tool: 'Read',
      detail: 'src/main.rs',
      id: 't2',
      at: '2026-08-19T10:00:16.000Z',
    },
  ];

  it('pairs each call with its result to derive a window', () => {
    const [first] = toolTimings(events);
    expect(first.tool).toBe('Bash');
    expect(first.ms).toBe(15_000);
  });

  it('keeps a call that never returned, rather than dropping it', () => {
    const timings = toolTimings(events);
    expect(timings).toHaveLength(2);
    expect(timings[1].ms).toBeNull();
  });

  it('marks a failed call from either the result or the tool row', () => {
    const failed: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'exit 1',
        id: 't9',
        at: '2026-08-19T10:00:00.000Z',
      },
      {
        kind: 'tool_result',
        text: 'boom',
        toolUseId: 't9',
        isError: true,
        at: '2026-08-19T10:00:01.000Z',
      },
    ];
    expect(toolTimings(failed)[0].isError).toBe(true);
  });
});

describe('TurnFooter', () => {
  it('uses the selected attempt telemetry instead of the newest retry', () => {
    const first = { ...RESOURCES, cpuUsec: 1_000_000 };
    const second = { ...RESOURCES, cpuUsec: 7_000_000 };
    render(
      <TurnFooter
        attemptNo={1}
        job={job({
          continuityMode: 'warm_native',
          durationMs: 9_000,
          resources: second,
          attempts: [
            {
              no: 1,
              status: 'superseded',
              telemetry: {
                continuityMode: 'replay',
                contextStatus: 'lost',
                durationMs: 3_000,
                resources: first,
              },
            },
            {
              no: 2,
              status: 'finished',
              telemetry: {
                continuityMode: 'warm_native',
                contextStatus: 'restored',
                durationMs: 9_000,
                resources: second,
              },
            },
          ],
        })}
      />,
    );

    expect(screen.queryByRole('button', { name: 'context lost' })).toBeNull();
    expect(screen.getByText('3s')).toBeInTheDocument();
    expect(screen.getByText('1s')).toBeInTheDocument();
    expect(screen.queryByText('9s')).toBeNull();
    expect(screen.queryByText('7s')).toBeNull();
  });

  it.each<Partial<AgentJob>>([
    { contextStatus: 'new' },
    { contextStatus: 'live' },
    { contextStatus: 'restored', continuityMode: 'warm_native' },
    { contextStatus: 'lost', continuityMode: 'replay' },
    { continuityMode: 'hot' },
    { continuityMode: 'warm_native' },
    { continuityMode: 'cold_native' },
    { continuityMode: 'replay' },
  ])('keeps duration and resources without internal session badges: %j', (telemetry) => {
    const { container, rerender } = render(
      <TurnFooter job={job({ ...telemetry, durationMs: 42_000, resources: RESOURCES })} />,
    );

    expect(screen.getByText('42s')).toBeInTheDocument();
    expect(screen.getByText('18.2s')).toBeInTheDocument();
    expect(screen.getByText('1.2GB')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(container.querySelector('[title]')).toBeNull();
    expect(
      screen.queryByText(/^(new|context live|restored|context lost|hot|warm|cold|replay)$/),
    ).toBeNull();

    // Session telemetry alone must not leave an empty footer behind.
    rerender(<TurnFooter job={job(telemetry)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('closes a resource explanation when its chip is clicked again', () => {
    render(<TurnFooter job={job({ resources: { ...RESOURCES, coverage: 0.4 } })} />);
    const badge = screen.getByRole('button', { name: 'Partial usage' });

    fireEvent.click(badge);
    expect(screen.getByText(/Usage is available for 40% of this turn/)).toBeInTheDocument();
    fireEvent.click(badge);

    expect(screen.queryByText(/Usage is available for 40% of this turn/)).toBeNull();
  });

  it('calls a partly sampled turn a floor, not a total', () => {
    render(<TurnFooter job={job({ resources: { ...RESOURCES, coverage: 0.4 } })} />);

    fireEvent.click(screen.getByRole('button', { name: 'Partial usage' }));

    expect(screen.getByText(/Usage is available for 40% of this turn/)).toBeInTheDocument();
    expect(screen.getByText(/Actual usage may be higher/)).toBeInTheDocument();
  });

  it('omits unavailable totals rather than showing zeros', () => {
    const unmeasured: TurnResources = {
      cpuUsec: null,
      memPeakBytes: null,
      memAvgBytes: null,
      ioReadBytes: null,
      ioWriteBytes: null,
      netRxBytes: null,
      netTxBytes: null,
      sampleCount: 0,
      samplerVersion: 'shared-member-v1',
      coverage: 0,
      toolTimeMs: null,
      toolCalls: [],
      toolCallsTruncated: false,
    };
    const { container, rerender } = render(<TurnFooter job={job({ resources: unmeasured })} />);
    expect(container).toBeEmptyDOMElement();

    rerender(<TurnFooter job={job({ resources: unmeasured, durationMs: 12_000 })} />);
    expect(screen.getByText('12s')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByText(/^0/)).toBeNull();
  });

  it('omits an empty footer when historical resource totals are unavailable', () => {
    const { container } = render(
      <TurnFooter
        job={job({
          resources: {
            ...RESOURCES,
            cpuUsec: null,
            memPeakBytes: null,
            memAvgBytes: null,
            netRxBytes: null,
            netTxBytes: null,
            sampleCount: 0,
            coverage: 0,
          },
        })}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing at all when there is nothing honest to say', () => {
    const { container, rerender } = render(<TurnFooter job={job()} />);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <TurnFooter
        job={job({
          resources: toTurnResources({
            sampler_version: 'shared-member-v1',
            process_observer: 'kata-guest-ebpf-v1',
            process_observation_complete: true,
            processes: [],
          } as never),
        })}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('opens the per-tool table and labels its timing as approximate', () => {
    const withTools = job({
      events: [
        {
          kind: 'tool_use',
          tool: 'Bash',
          detail: 'cargo build',
          id: 't1',
          at: '2026-08-19T10:00:00.000Z',
        },
        {
          kind: 'tool_result',
          text: 'ok',
          toolUseId: 't1',
          isError: false,
          at: '2026-08-19T10:00:15.000Z',
        },
      ],
    });
    render(<TurnFooter job={withTools} />);
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }));
    expect(screen.getByText('cargo build')).toBeInTheDocument();
    expect(screen.getByText('15s')).toBeInTheDocument();
    expect(screen.getByTitle('Approximate duration')).toHaveTextContent('15s');
  });
});

describe('the per-tool table on real data', () => {
  // The adapter folds a tool_result into its tool_use row and drops the
  // result, so a footer that paired the two itself found nothing to pair and
  // rendered every call as unfinished — in production, while every test that
  // hand-built both rows passed. These go through the real adapter.
  const apiJob = {
    id: 'ajob_1',
    repo: 'o/r',
    task_prompt: 'build it',
    runtime: 'claude-code',
    model: 'glm-4.6',
    base_sha: 'abc1234',
    state: 'done',
    cancel_requested: false,
    current_attempt_id: 7,
    telemetry_attempt_id: 7,
    published_pr_url: null,
    detail: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    created_at: '2026-08-19T10:00:00.000Z',
    updated_at: '2026-08-19T10:01:00.000Z',
  } as never;

  const apiEvents = [
    {
      id: 1,
      attempt_id: 7,
      seq: 1,
      event_type: 'tool_use',
      payload: { id: 't1', name: 'Bash', detail: 'cargo build' },
      created_at: '2026-08-19T10:00:00.000Z',
    },
    {
      id: 2,
      attempt_id: 7,
      seq: 2,
      event_type: 'tool_result',
      payload: { tool_use_id: 't1', content: 'compiled' },
      created_at: '2026-08-19T10:00:15.000Z',
    },
  ] as never;

  it('times a call whose result the adapter folded away', () => {
    const adapted = toDisplayJob(apiJob, { events: apiEvents });

    const [timing] = toolTimings(adapted.events);

    expect(timing.tool).toBe('Bash');
    expect(timing.ms).toBe(15_000);
  });

  it('renders that duration in the footer instead of "unfinished"', () => {
    const adapted = toDisplayJob(apiJob, { events: apiEvents });

    render(<TurnFooter job={adapted} />);
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }));

    expect(screen.getByText('15s')).toBeInTheDocument();
    expect(screen.queryByText('unfinished')).toBeNull();
  });
});

describe('per-call resources, through both real adapters', () => {
  // Events go through `toDisplayJob` and resources through `toTurnResources`,
  // because both are where a field is silently lost: a footer fed hand-built
  // objects passes while the shipped one renders nothing.
  const apiJob = {
    id: 'ajob_1',
    repo: 'o/r',
    task_prompt: 'build it',
    runtime: 'claude-code',
    model: 'glm-4.6',
    state: 'done',
    cancel_requested: false,
    current_attempt_id: 7,
    published_pr_url: null,
    detail: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    created_at: '2026-08-19T10:00:00.000Z',
    updated_at: '2026-08-19T10:01:00.000Z',
  } as never;

  const apiEvents = [
    {
      id: 1,
      attempt_id: 7,
      seq: 1,
      event_type: 'tool_use',
      payload: { id: 'long', name: 'Bash', detail: 'cargo build' },
      created_at: '2026-08-19T10:00:00.000Z',
    },
    {
      id: 2,
      attempt_id: 7,
      seq: 2,
      event_type: 'tool_result',
      payload: { tool_use_id: 'long', content: 'compiled' },
      created_at: '2026-08-19T10:00:09.500Z',
    },
    {
      id: 3,
      attempt_id: 7,
      seq: 3,
      event_type: 'tool_use',
      payload: { id: 'short', name: 'Read', detail: 'src/main.rs' },
      created_at: '2026-08-19T10:00:10.000Z',
    },
    {
      id: 4,
      attempt_id: 7,
      seq: 4,
      event_type: 'tool_result',
      payload: { tool_use_id: 'short', content: 'fn main' },
      created_at: '2026-08-19T10:00:10.035Z',
    },
  ] as never;

  function withCalls(
    calls: unknown[],
    truncated = false,
    resourceOverrides: Record<string, unknown> = {},
  ) {
    const adapted = toDisplayJob(apiJob, { events: apiEvents });
    return {
      ...adapted,
      resources: toTurnResources({
        cpu_usec: 5_000_000,
        mem_peak_bytes: 40_000_000,
        mem_avg_bytes: null,
        io_read_bytes: null,
        io_write_bytes: null,
        net_rx_bytes: null,
        net_tx_bytes: null,
        sample_count: 12,
        coverage: 1,
        sampler_version: '2',
        tool_calls: calls,
        tool_calls_truncated: truncated,
        ...resourceOverrides,
      } as never),
    };
  }

  const measuredLongCall = {
    call_seq: 1,
    tool_use_id: 'long',
    tool_name: 'Bash',
    timing_source: 'message',
    duration_ms: 9_500,
    sample_count: 9,
    overlap_n: 1,
    measured_span_ms: 8_000,
    cpu_usec_exclusive: 4_400_000,
    cpu_usec_shared: null,
    mem_peak_exclusive_bytes: 283_115_520,
    mem_peak_shared_bytes: null,
  };

  const unmeasuredShortCall = {
    call_seq: 2,
    tool_use_id: 'short',
    tool_name: 'Read',
    timing_source: 'message',
    duration_ms: 35,
    sample_count: 0,
    overlap_n: 1,
    measured_span_ms: null,
    cpu_usec_exclusive: null,
    cpu_usec_shared: null,
    mem_peak_exclusive_bytes: null,
    mem_peak_shared_bytes: null,
  };

  const tinyProcess = {
    process_seq: 1,
    uid: 11001,
    pid: 412,
    tgid: 412,
    ppid: 41,
    comm: 'tiny',
    executable: '/bin/tiny',
    started_offset_ms: 12,
    duration_ms: 1,
    exit_code: 0,
    observed_fork: true,
    observed_exec: true,
    observed_exit: true,
    tool_call_seqs: [1],
  };

  function processOnlyResources(resourceOverrides: Record<string, unknown> = {}) {
    return toTurnResources({
      cpu_usec: null,
      mem_peak_bytes: null,
      mem_avg_bytes: null,
      io_read_bytes: null,
      io_write_bytes: null,
      net_rx_bytes: null,
      net_tx_bytes: null,
      sample_count: 0,
      coverage: 0,
      sampler_version: 'shared-member-v1',
      tool_calls: [],
      tool_calls_truncated: false,
      process_observer: 'kata-guest-ebpf-v1',
      process_observation_complete: true,
      process_events_lost: 0,
      processes_truncated: false,
      processes: [],
      ...resourceOverrides,
    } as never);
  }

  function openTable(job: AgentJob) {
    render(<TurnFooter job={job} />);
    fireEvent.click(screen.getByRole('button', { name: '2 tool calls' }));
  }

  it('shows what the measured call consumed', () => {
    openTable(withCalls([measuredLongCall, unmeasuredShortCall]));

    expect(screen.getByText('4.4s')).toBeInTheDocument();
    expect(screen.getByText('270MB')).toBeInTheDocument();
  });

  it('shows a dash for the call too short to measure, never a zero', () => {
    openTable(withCalls([measuredLongCall, unmeasuredShortCall]));

    // CPU and memory are unmeasured; process counts are also unknown because
    // this fixture predates the guest observer. None of those is a measured 0.
    expect(screen.getAllByText('—')).toHaveLength(4);
    expect(screen.getAllByTitle('Not available').length).toBeGreaterThan(0);
    expect(screen.queryByText('0s')).toBeNull();
    expect(screen.queryByText('0B')).toBeNull();
  });

  it('marks a call that shared its reading as an upper bound', () => {
    const shared = {
      ...measuredLongCall,
      overlap_n: 2,
      cpu_usec_exclusive: null,
      cpu_usec_shared: 4_400_000,
      mem_peak_exclusive_bytes: null,
      mem_peak_shared_bytes: 283_115_520,
    };
    openTable(withCalls([shared, unmeasuredShortCall]));

    expect(screen.getByText('4.4s')).toBeInTheDocument();
    expect(screen.getAllByTitle(/upper bound/)[0]).toBeInTheDocument();
  });

  it('does not claim an upper bound when nothing was shared', () => {
    openTable(withCalls([measuredLongCall, unmeasuredShortCall]));

    expect(screen.queryByTitle(/upper bound/)).toBeNull();
  });

  it('names the longest call so a 157-row table does not have to be read', () => {
    openTable(withCalls([measuredLongCall, unmeasuredShortCall]));

    expect(screen.getByText(/Longest call:/)).toBeInTheDocument();
    expect(screen.getAllByText('Bash').length).toBeGreaterThan(1);
  });

  it('renders and ranks the recorded duration instead of the wider event window', () => {
    const exactLong = { ...measuredLongCall, timing_source: 'exact', duration_ms: 10 };
    openTable(withCalls([exactLong, unmeasuredShortCall]));

    expect(screen.getByText('10ms')).toBeInTheDocument();
    expect(screen.queryByText('9.5s')).toBeNull();
    expect(screen.getByText(/Longest call:/)).toHaveTextContent('Read · 35ms');
  });

  it('says when the list is not every call the turn made', () => {
    openTable(
      withCalls([measuredLongCall, unmeasuredShortCall], true, {
        tool_time_ms: 9_535,
      }),
    );

    expect(screen.getByText(/more calls than are listed/)).toBeInTheDocument();
    expect(screen.queryByText('4.4s')).toBeNull();
    expect(screen.queryByText(/on other work/)).toBeNull();
  });

  it('says where the turn\u2019s time actually went', () => {
    const withSplit = withCalls([measuredLongCall, unmeasuredShortCall]);
    render(
      <TurnFooter
        job={{
          ...withSplit,
          durationMs: 3_559_000,
          resources: { ...withSplit.resources!, toolTimeMs: 49_600 },
        }}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '2 tool calls' }));

    // The most explanatory number on the footer: on the production turns this
    // was built against, the sandbox was busy for about 1% of the wall clock.
    expect(screen.getByText(/on other work/)).toBeInTheDocument();
    expect(screen.getByText('49.6s')).toBeInTheDocument();
  });

  it('does not invent a split when the tool time was never measured', () => {
    const withSplit = withCalls([measuredLongCall]);
    render(<TurnFooter job={{ ...withSplit, durationMs: 3_559_000 }} />);
    fireEvent.click(screen.getByRole('button', { name: '2 tool calls' }));

    expect(screen.queryByText(/on other work/)).toBeNull();
  });

  it('marks message-level durations as approximate without timing-source prose', () => {
    openTable(withCalls([measuredLongCall, unmeasuredShortCall]));

    expect(screen.getAllByTitle('Approximate duration')).toHaveLength(2);
  });

  it('does not mark directly measured durations as approximate', () => {
    const exact = { ...measuredLongCall, timing_source: 'exact' };
    const exactShort = { ...unmeasuredShortCall, timing_source: 'exact' };
    openTable(withCalls([exact, exactShort]));

    expect(screen.queryByTitle('Approximate duration')).toBeNull();
    expect(
      within(screen.getByText('cargo build').closest('tr')!).getByText('9.5s'),
    ).toBeInTheDocument();
  });

  it('only marks durations that were not directly measured as approximate', () => {
    const exact = { ...measuredLongCall, timing_source: 'exact' };
    openTable(withCalls([exact, unmeasuredShortCall]));

    expect(
      within(screen.getByText('cargo build').closest('tr')!).queryByTitle('Approximate duration'),
    ).toBeNull();
    expect(
      within(screen.getByText('src/main.rs').closest('tr')!).getByTitle('Approximate duration'),
    ).toHaveTextContent('35ms');
  });

  it('never reuses one recorded row for two malformed duplicate tool ids', () => {
    const duplicateEvents: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'first duplicate',
        id: 'duplicate',
        durationMs: 100,
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'second duplicate',
        id: 'duplicate',
        durationMs: 200,
      },
    ];
    render(
      <TurnFooter
        job={job({
          events: duplicateEvents,
          resources: {
            ...RESOURCES,
            toolCallsTruncated: true,
            toolCalls: [
              {
                toolUseId: 'duplicate',
                toolName: 'Bash',
                timingSource: 'exact',
                durationMs: 10,
                sampleCount: 0,
                overlapN: 1,
                measuredSpanMs: null,
                cpuUsec: null,
                memPeakBytes: null,
                exclusive: false,
                processCount: 0,
              },
            ],
          },
        })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '2 tool calls' }));

    expect(
      within(screen.getByText('first duplicate').closest('tr')!).getByText('10ms'),
    ).toBeInTheDocument();
    expect(
      within(screen.getByText('second duplicate').closest('tr')!).getByTitle(
        'Approximate duration',
      ),
    ).toBeInTheDocument();
  });

  it('renders dashes rather than nothing when no measurement was reported', () => {
    // Every turn recorded before this shipped, and every turn on a host whose
    // daemon could not be read.
    openTable(withCalls([]));

    expect(screen.getAllByText('—')).toHaveLength(6);
    expect(screen.getAllByTitle('Not available').length).toBeGreaterThan(0);
  });

  it('renders a sub-tick process delivered by the guest observer', () => {
    openTable(
      withCalls([{ ...measuredLongCall, process_count: 1 }, unmeasuredShortCall], false, {
        process_observer: 'kata-guest-ebpf-v1',
        process_observation_complete: true,
        process_events_lost: 0,
        processes_truncated: false,
        processes: [tinyProcess],
      }),
    );

    expect(screen.getByRole('columnheader', { name: 'Program' })).toBeInTheDocument();
    expect(screen.getByText('/bin/tiny')).toBeInTheDocument();
    expect(screen.getByText('+12ms')).toBeInTheDocument();
    expect(screen.queryByText(/Process data is incomplete/)).toBeNull();
  });

  it('does not present a lossy guest observation as a complete process census', () => {
    openTable(
      withCalls([{ ...measuredLongCall, process_count: 1 }, unmeasuredShortCall], false, {
        process_observer: 'kata-guest-ebpf-v1',
        process_observation_complete: false,
        process_events_lost: 2,
        processes_truncated: false,
        processes: [tinyProcess],
      }),
    );

    expect(screen.getByText(/Process data is incomplete/)).toBeInTheDocument();
    expect(screen.getByText('≥1')).toBeInTheDocument();
  });

  it('describes non-loss observer incompleteness without blaming the kernel ring', () => {
    render(
      <TurnFooter
        job={job({
          resources: processOnlyResources({
            process_observation_complete: false,
            processes_truncated: true,
          }),
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Process details' }));

    expect(screen.getByText(/Process data is incomplete/)).toBeInTheDocument();
  });

  it('keeps guest process details accessible when a turn emitted no tool event', () => {
    render(
      <TurnFooter
        job={job({
          resources: processOnlyResources({ processes: [tinyProcess] }),
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Process details' }));

    expect(screen.getByRole('columnheader', { name: 'Program' })).toBeInTheDocument();
    expect(screen.queryByText('Processes')).toBeNull();
    expect(screen.getByText('/bin/tiny')).toBeInTheDocument();
  });

  it('shows loss explicitly even when no process row survived', () => {
    render(
      <TurnFooter
        job={job({
          resources: processOnlyResources({
            // Even if an older/bad row claims completeness, explicit loss is
            // authoritative and the adapter must keep the UI conservative.
            process_observation_complete: true,
            process_events_lost: 1,
          }),
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Process details' }));

    expect(screen.getByText(/Process data is incomplete/)).toBeInTheDocument();
    expect(screen.queryByText(/No processes were recorded/)).toBeNull();
  });

  const accountedProcess = {
    ...tinyProcess,
    executable: '/usr/bin/dd',
    cpu_usec: 15_000,
    mem_peak_bytes: 2_228_224,
    io_read_bytes: 0,
    io_write_bytes: 8_388_608,
    io_rchar_bytes: 8_390_683,
    io_wchar_bytes: 8_388_701,
  };

  it('shows what each guest process consumed at exit', () => {
    render(
      <TurnFooter
        job={job({
          resources: processOnlyResources({ processes: [accountedProcess] }),
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Process details' }));

    const row = screen.getByText('/usr/bin/dd').closest('tr')!;
    expect(within(row).getByText('15ms')).toBeInTheDocument();
    expect(within(row).getByText('2.1MB')).toBeInTheDocument();
    expect(within(row).getByText('r0B w8MB')).toBeInTheDocument();
    expect(screen.queryByText(/guest kernel/i)).toBeNull();
  });

  it('leaves consumption blank for a process that had not exited', () => {
    render(
      <TurnFooter
        job={job({
          resources: processOnlyResources({
            processes: [
              { ...tinyProcess, duration_ms: null, exit_code: null, observed_exit: false },
            ],
          }),
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Process details' }));

    const row = screen.getByText('/bin/tiny').closest('tr')!;
    expect(within(row).getByText('still running')).toBeInTheDocument();
    // CPU, peak RSS, I/O and exit code: four dashes, no zeros.
    expect(within(row).getAllByText('—')).toHaveLength(4);
  });

  it('charges a call too short for the sampler with what its own processes consumed', () => {
    openTable(
      withCalls(
        [
          measuredLongCall,
          {
            ...unmeasuredShortCall,
            process_count: 1,
            process_cpu_usec: 15_000,
            process_mem_peak_bytes: 2_228_224,
            process_io_read_bytes: 0,
            process_io_write_bytes: 8_388_608,
            process_resources_partial: false,
          },
        ],
        false,
        {
          process_observer: 'kata-guest-ebpf-v1',
          process_observation_complete: true,
          process_events_lost: 0,
          processes_truncated: false,
          processes: [{ ...accountedProcess, tool_call_seqs: [2] }],
        },
      ),
    );

    const shortRow = screen.getByText('Read').closest('tr')!;
    expect(within(shortRow).getByText('15ms')).toBeInTheDocument();
    expect(within(shortRow).getByText('2.1MB')).toBeInTheDocument();
    expect(within(shortRow).getAllByText('ᵖ')).toHaveLength(2);
    // The sampled call keeps its sampled numbers, unmarked.
    const longRow = screen.getByText('4.4s').closest('tr')!;
    expect(within(longRow).getByText('270MB')).toBeInTheDocument();
    expect(within(longRow).queryByText('ᵖ')).toBeNull();
    expect(within(shortRow).getAllByTitle(/Child processes only/)).toHaveLength(2);
    expect(screen.getByText('5s')).toBeInTheDocument();
  });

  it('marks a call whose processes were not all its own as a floor', () => {
    openTable(
      withCalls(
        [
          measuredLongCall,
          {
            ...unmeasuredShortCall,
            process_count: 2,
            process_cpu_usec: 15_000,
            process_mem_peak_bytes: null,
            process_io_read_bytes: null,
            process_io_write_bytes: null,
            process_resources_partial: true,
          },
        ],
        false,
        {
          process_observer: 'kata-guest-ebpf-v1',
          process_observation_complete: true,
          process_events_lost: 0,
          processes_truncated: false,
          processes: [{ ...accountedProcess, tool_call_seqs: [2] }],
        },
      ),
    );

    const shortRow = screen.getByText('Read').closest('tr')!;
    expect(within(shortRow).getByText('≥15ms')).toBeInTheDocument();
    expect(within(shortRow).getByTitle(/At least this much/)).toHaveTextContent('≥15ms');
  });

  it('treats every charged call as a floor when the census lost events', () => {
    // The host marks this too; the adapter repeats it so an older or wrong
    // row can never present an exit-accounted total beside a lossy census.
    openTable(
      withCalls(
        [
          measuredLongCall,
          {
            ...unmeasuredShortCall,
            process_count: 1,
            process_cpu_usec: 15_000,
            process_mem_peak_bytes: 2_228_224,
            process_resources_partial: false,
          },
        ],
        false,
        {
          process_observer: 'kata-guest-ebpf-v1',
          process_observation_complete: true,
          process_events_lost: 2,
          processes_truncated: false,
          processes: [{ ...accountedProcess, tool_call_seqs: [2] }],
        },
      ),
    );

    const shortRow = screen.getByText('Read').closest('tr')!;
    expect(within(shortRow).getByText('≥15ms')).toBeInTheDocument();
    expect(within(shortRow).getByText('≥2.1MB')).toBeInTheDocument();
  });

  it('never lets a truncated call list keep exit-accounted numbers', () => {
    openTable(
      withCalls(
        [
          measuredLongCall,
          {
            ...unmeasuredShortCall,
            process_count: 1,
            process_cpu_usec: 15_000,
            process_mem_peak_bytes: 2_228_224,
            process_resources_partial: false,
          },
        ],
        true,
        {
          process_observer: 'kata-guest-ebpf-v1',
          process_observation_complete: true,
          process_events_lost: 0,
          processes_truncated: false,
          processes: [{ ...accountedProcess, tool_call_seqs: [2] }],
        },
      ),
    );

    const shortRow = screen.getByText('Read').closest('tr')!;
    expect(within(shortRow).queryByText('15ms')).toBeNull();
    expect(within(shortRow).queryByText('ᵖ')).toBeNull();
  });
});

describe('attempt scoping', () => {
  it('times only the attempt the reader is looking at', () => {
    const events: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'first try',
        id: 'dup',
        attemptNo: 1,
        durationMs: 1_000,
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'second try',
        id: 'dup',
        attemptNo: 2,
        durationMs: 9_000,
      },
    ];

    expect(toolTimings(events, 2).map((t) => t.detail)).toEqual(['second try']);
    expect(toolTimings(events, 1).map((t) => t.ms)).toEqual([1_000]);
  });

  it('does not attach the newest attempt telemetry to an older attempt', () => {
    const events: AgentEvent[] = [
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'first try',
        id: 'dup',
        attemptNo: 1,
        durationMs: 1_000,
      },
      {
        kind: 'tool_use',
        tool: 'Bash',
        detail: 'second try',
        id: 'dup',
        attemptNo: 2,
        durationMs: 9_000,
      },
    ];
    render(
      <TurnFooter
        attemptNo={1}
        job={job({
          attempts: [
            { no: 1, status: 'superseded' },
            { no: 2, status: 'finished' },
          ],
          telemetryAttemptNo: 2,
          continuityMode: 'hot',
          contextStatus: 'live',
          durationMs: 42_000,
          resources: RESOURCES,
          events,
        })}
      />,
    );

    expect(screen.queryByText('hot')).not.toBeInTheDocument();
    expect(screen.queryByText('context live')).not.toBeInTheDocument();
    expect(screen.queryByText('42s')).not.toBeInTheDocument();
    expect(screen.queryByText('18.2s')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }));
    expect(screen.getByText('1s')).toBeInTheDocument();
    expect(screen.queryByText('9s')).not.toBeInTheDocument();
  });
});
