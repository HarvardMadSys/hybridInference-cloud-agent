// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
// No user-event dependency in this repo; fireEvent is the house click.
import { afterEach, describe, expect, it } from 'vitest';

import { toDisplayJob } from './adapt';
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
  coverage: 1,
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
    networkSetup: '',
    networkAgent: '',
    sandbox: '',
    attempts: [],
    events: [],
    eventCount: 0,
    diffFiles: [],
    diffLines: [],
    rawLines: [],
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 0 },
    egressDenials: 0,
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
  it('shows the continuity rung and what the turn consumed', () => {
    render(
      <TurnFooter job={job({ continuityMode: 'hot', durationMs: 42_000, resources: RESOURCES })} />,
    );
    expect(screen.getByText('hot')).toBeInTheDocument();
    expect(screen.getByText('42s')).toBeInTheDocument();
    expect(screen.getByText('18.2s')).toBeInTheDocument();
    expect(screen.getByText('1.2GB')).toBeInTheDocument();
  });

  it('explains a replay turn on demand, which is why the badge exists', () => {
    // A native `title` was the first delivery and it was the wrong one: it
    // wants a second of motionless hover and cannot be read on a touch
    // screen, so the footer's most important explanation was the least
    // likely to be seen. Clicking always works.
    render(<TurnFooter job={job({ continuityMode: 'replay' })} />);

    expect(screen.queryByText(/working memory/i)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'replay' }));

    expect(screen.getByText(/working memory/i)).toBeInTheDocument();
    expect(screen.getByText(/written transcript/i)).toBeInTheDocument();
  });

  it('closes an explanation when its chip is clicked again', () => {
    render(<TurnFooter job={job({ continuityMode: 'replay' })} />);
    const badge = screen.getByRole('button', { name: 'replay' });

    fireEvent.click(badge);
    fireEvent.click(badge);

    expect(screen.queryByText(/working memory/i)).toBeNull();
  });

  it('shows one explanation at a time', () => {
    render(
      <TurnFooter
        job={job({ continuityMode: 'replay', resources: { ...RESOURCES, coverage: 0.4 } })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'replay' }));
    fireEvent.click(screen.getByRole('button', { name: 'partial' }));

    expect(screen.getByText(/40% of this turn was sampled/)).toBeInTheDocument();
    expect(screen.queryByText(/working memory/i)).toBeNull();
  });

  it('calls a partly sampled turn a floor, not a total', () => {
    render(<TurnFooter job={job({ resources: { ...RESOURCES, coverage: 0.4 } })} />);

    fireEvent.click(screen.getByRole('button', { name: 'partial' }));

    expect(screen.getByText(/40% of this turn was sampled/)).toBeInTheDocument();
    expect(screen.getByText(/lower bounds rather than totals/)).toBeInTheDocument();
  });

  it('says nothing was measured rather than showing zeros', () => {
    const unmeasured: TurnResources = {
      cpuUsec: null,
      memPeakBytes: null,
      memAvgBytes: null,
      ioReadBytes: null,
      ioWriteBytes: null,
      netRxBytes: null,
      netTxBytes: null,
      sampleCount: 0,
      coverage: 0,
    };
    render(<TurnFooter job={job({ resources: unmeasured })} />);
    expect(screen.getByText('resource data unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/^0/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'resource data unavailable' }));
    expect(screen.getByText(/not the same as a turn that consumed nothing/)).toBeInTheDocument();
  });

  it('renders nothing at all when there is nothing honest to say', () => {
    const { container } = render(<TurnFooter job={job()} />);
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
    expect(screen.getByText(/include the model’s own thinking time/)).toBeInTheDocument();
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
    published_pr_url: null,
    detail: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    setup_egress_tier: null,
    agent_egress_tier: null,
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
});
