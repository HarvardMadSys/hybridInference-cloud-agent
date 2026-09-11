// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentAttemptTelemetry } from '@/lib/api/agents';
import type { AgentThreadEventApi } from '@/lib/api/agents';

import {
  TrajectoryInspector,
  type InspectorSelection,
  type RunTab,
  type StepTab,
} from './TrajectoryInspector';
import { buildTrajectorySteps, type StoredEvent } from './trajectorySteps';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({
  getAgentAttemptTelemetry: vi.fn(),
  getAgentJobUsage: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(getAgentAttemptTelemetry).mockReset();
  vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
    attempt_id: 1,
    attempt_no: 1,
    continuity_mode: null,
    context_status: null,
    duration_ms: null,
    resources: null,
  });
});

afterEach(cleanup);

function event(over: Partial<AgentThreadEventApi> & { id: number }): AgentThreadEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    job_id: 'ajob_1',
    turn_no: 7,
    ...over,
  } as AgentThreadEventApi;
}

const CURRENT_JOB = {
  id: 'ajob_1',
  runtime: 'claude-code',
  model: 'qwen-test',
  turnNo: 7,
} as AgentJob;

function Harness({
  events,
  initial,
  query = '',
  onClearQuery = vi.fn(),
  jobs = [],
}: {
  events: StoredEvent[];
  initial: InspectorSelection;
  query?: string;
  onClearQuery?: () => void;
  jobs?: never[];
}) {
  const [selection, setSelection] = useState<InspectorSelection>(initial);
  const steps = buildTrajectorySteps(events);
  return (
    <TrajectoryInspector
      selection={selection}
      steps={steps}
      events={events}
      jobs={jobs}
      currentJob={CURRENT_JOB}
      query={query}
      onClearQuery={onClearQuery}
      onSelectTab={(tab: StepTab | RunTab) =>
        setSelection((current) =>
          current.kind === 'step'
            ? { ...current, tab: tab as StepTab }
            : { ...current, tab: tab as RunTab },
        )
      }
      onSelectStep={(key) => setSelection({ kind: 'step', stepKey: key, tab: 'summary' })}
      onClose={vi.fn()}
      onOpenGitDiff={vi.fn()}
    />
  );
}

/** A Claude-shaped message with one tool call that answered successfully. */
const ASSISTANT_WITH_TOOL = [
  event({
    id: 1,
    event_type: 'thinking',
    payload: { text: 'checking the tree', usage_id: 'msg_1', usage: { output_tokens: 120 } },
  }),
  event({
    id: 2,
    event_type: 'tool_use',
    payload: {
      id: 't1',
      name: 'Bash',
      input: { command: 'pytest -q' },
      usage_id: 'msg_1',
      usage: { output_tokens: 120 },
    },
    created_at: '2026-08-19T10:00:00.000Z',
  }),
  event({
    id: 3,
    event_type: 'tool_result',
    payload: { tool_use_id: 't1', content: '2 passed', exit_code: 0 },
    created_at: '2026-08-19T10:00:02.000Z',
  }),
  event({
    id: 4,
    payload: { text: 'the tests pass', usage_id: 'msg_1', usage: { output_tokens: 120 } },
  }),
];

function toolKeyOf(events: StoredEvent[]): string {
  const step = buildTrajectorySteps(events).find((candidate) => candidate.kind === 'tool');
  return step?.key ?? '';
}

/** One sandbox measurement of the fixture's call, as the attempt records it. */
function measuredCall(durationMs: number) {
  return {
    call_seq: 1,
    tool_use_id: 't1',
    tool_name: 'Bash',
    timing_source: 'exact',
    duration_ms: durationMs,
    sample_count: 0,
    overlap_n: 1,
    measured_span_ms: null,
    cpu_usec_exclusive: null,
    cpu_usec_shared: null,
    mem_peak_exclusive_bytes: null,
    mem_peak_shared_bytes: null,
  };
}

function attemptTelemetry(toolCalls: Array<ReturnType<typeof measuredCall>>) {
  return {
    attempt_id: 1,
    attempt_no: 1,
    continuity_mode: null,
    context_status: null,
    duration_ms: null,
    resources: {
      cpu_usec: null,
      mem_peak_bytes: null,
      mem_avg_bytes: null,
      io_read_bytes: null,
      io_write_bytes: null,
      net_rx_bytes: null,
      net_tx_bytes: null,
      sample_count: 0,
      coverage: 1,
      sampler_version: null,
      tool_calls: toolCalls,
    },
  };
}

/** One call id stored twice in the same attempt, answered twice. */
const REUSED_CALL = [
  event({ id: 1, event_type: 'tool_use', payload: { id: 't1', name: 'Bash' } }),
  event({ id: 2, event_type: 'tool_result', payload: { tool_use_id: 't1', content: 'first' } }),
  event({ id: 3, event_type: 'tool_use', payload: { id: 't1', name: 'Bash' } }),
  event({ id: 4, event_type: 'tool_result', payload: { tool_use_id: 't1', content: 'second' } }),
];

describe('TrajectoryInspector', () => {
  it('gives an assistant message exactly Summary, Preview and Raw', () => {
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{
          kind: 'step',
          stepKey: buildTrajectorySteps(ASSISTANT_WITH_TOOL)[0].key,
          tab: 'summary',
        }}
      />,
    );

    const tabs = screen.getAllByRole('tab').map((tab) => tab.textContent);
    expect(tabs).toEqual(['Summary', 'Preview', 'Raw']);
    expect(screen.getByText(/Assistant · Turn 7 · Step 1/)).toBeInTheDocument();
  });

  it('gives a tool call exactly Summary, Payload, Result, Schema and Timing', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'summary' }}
      />,
    );

    const tabs = screen.getAllByRole('tab').map((tab) => tab.textContent);
    expect(tabs).toEqual(['Summary', 'Payload', 'Result', 'Schema', 'Timing']);
    expect(screen.getByText(/Tool · Turn 7/)).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Preview' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Raw' })).toBeNull();
  });

  it('shows the tool summary sections and jumps from each one to its tab', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'summary' }}
      />,
    );

    expect(screen.getByText('Hierarchy')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    const payloadSection = screen
      .getByRole('heading', { level: 4, name: 'Payload' })
      .closest('section') as HTMLElement;
    expect(within(payloadSection).getByText(/pytest -q/)).toBeInTheDocument();

    fireEvent.click(within(payloadSection).getByRole('button', { name: 'Open tab' }));

    expect(screen.getByRole('tab', { name: 'Payload' })).toHaveAttribute('aria-selected', 'true');
  });

  it('jumps from Hierarchy to the assistant message that issued the call', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'summary' }}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Assistant message ›' }));

    expect(screen.getByText(/Assistant · Turn 7 · Step 1/)).toBeInTheDocument();
  });

  it('refuses to invent a hierarchy when no parent identity was recorded', () => {
    const events = [
      event({ id: 1, payload: { text: 'before' } }),
      event({
        id: 2,
        event_type: 'tool_use',
        payload: { id: 't9', name: 'Read', input: { file: 'a.ts' } },
      }),
    ];
    render(
      <Harness
        events={events}
        initial={{ kind: 'step', stepKey: toolKeyOf(events), tab: 'summary' }}
      />,
    );

    // Hierarchy is a labelled row in the summary, beside Status.
    expect(screen.getByText('Hierarchy').nextElementSibling).toHaveTextContent('Not recorded');
    expect(screen.queryByRole('button', { name: 'Assistant message ›' })).toBeNull();
  });

  it('keeps the Schema tab and says the definition was not recorded', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'schema' }}
      />,
    );

    expect(screen.getByText('Tool schema was not recorded for this call.')).toBeInTheDocument();
    expect(screen.getAllByText('Bash').length).toBeGreaterThan(0);
  });

  it('times a call from its records and says what that interval covers', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'timing' }}
      />,
    );

    expect(screen.getByText('Session timestamps')).toBeInTheDocument();
    expect(screen.getByText('2s')).toBeInTheDocument();
    expect(screen.getByText(/not pure tool execution time/)).toBeInTheDocument();
  });

  it('prefers the sandbox hooks when the attempt recorded exact timing', async () => {
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
      attempt_id: 1,
      attempt_no: 1,
      continuity_mode: null,
      context_status: null,
      duration_ms: null,
      resources: {
        cpu_usec: null,
        mem_peak_bytes: null,
        mem_avg_bytes: null,
        io_read_bytes: null,
        io_write_bytes: null,
        net_rx_bytes: null,
        net_tx_bytes: null,
        sample_count: 0,
        coverage: 1,
        sampler_version: null,
        tool_calls: [
          {
            call_seq: 1,
            tool_use_id: 't1',
            tool_name: 'Bash',
            timing_source: 'exact',
            duration_ms: 350,
            sample_count: 0,
            overlap_n: 1,
            measured_span_ms: null,
            cpu_usec_exclusive: null,
            cpu_usec_shared: null,
            mem_peak_exclusive_bytes: null,
            mem_peak_shared_bytes: null,
          },
        ],
      },
    });
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'timing' }}
      />,
    );

    await waitFor(() => expect(screen.getByText('Tool execution hooks')).toBeInTheDocument());
    expect(screen.getByText('350ms')).toBeInTheDocument();
  });

  it('measures a call only when the attempt recorded one measurement for it', async () => {
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue(
      attemptTelemetry([measuredCall(350), measuredCall(900)]),
    );
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'timing' }}
      />,
    );

    // Two readings for one call id name no single duration, so the tab falls
    // back to the interval the stored records bracket instead of picking one.
    await waitFor(() => expect(getAgentAttemptTelemetry).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('Session timestamps')).toBeInTheDocument());
    expect(screen.getByText('Duration').nextElementSibling).toHaveTextContent('2s');
    expect(screen.queryByText('350ms')).not.toBeInTheDocument();
    expect(screen.queryByText('900ms')).not.toBeInTheDocument();
  });

  it('attributes no measurement to a call whose records disagree', async () => {
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue(attemptTelemetry([measuredCall(350)]));
    const key = toolKeyOf(REUSED_CALL);
    render(
      <Harness events={REUSED_CALL} initial={{ kind: 'step', stepKey: key, tab: 'timing' }} />,
    );

    // The projection declines to claim a duration for this call, so the
    // sandbox's own measurement cannot put one back.
    await waitFor(() => expect(getAgentAttemptTelemetry).toHaveBeenCalled());
    await waitFor(() => expect(screen.getAllByText('Not recorded').length).toBeGreaterThan(1));
    expect(screen.getByText('Duration').nextElementSibling).toHaveTextContent('Not recorded');
    expect(screen.queryByText('350ms')).not.toBeInTheDocument();
    expect(screen.getByText(/records for this call disagree/)).toBeInTheDocument();
  });

  it('keeps unrecorded assistant usage empty rather than zero', () => {
    const events = [event({ id: 1, payload: { text: 'no usage here' } })];
    const key = buildTrajectorySteps(events)[0].key;
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'summary' }} />);

    expect(screen.getAllByText('Not recorded').length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText('First token unavailable')).toBeInTheDocument();
  });

  it('reports recorded message usage instead of the turn total', () => {
    const key = buildTrajectorySteps(ASSISTANT_WITH_TOOL)[0].key;
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'summary' }}
      />,
    );

    const tokens = screen.getByText('Tokens').closest('div') as HTMLElement;
    expect(within(tokens).getByText('120')).toBeInTheDocument();
    expect(screen.getByText('msg_1')).toBeInTheDocument();
  });

  it('marks a failed tool call and shows the exit code with its result', () => {
    const events = [
      event({
        id: 1,
        event_type: 'tool_use',
        payload: { id: 't1', name: 'Bash', input: 'pytest' },
      }),
      event({
        id: 2,
        event_type: 'tool_result',
        payload: { tool_use_id: 't1', content: '1 failed', exit_code: 1 },
      }),
    ];
    const key = toolKeyOf(events);
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'summary' }} />);

    expect(screen.getByText('Failed')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Result' }));

    expect(screen.getByText('1 failed')).toBeInTheDocument();
    expect(screen.getByText('Exit code')).toBeInTheDocument();
  });

  it('says an empty result is empty rather than missing', () => {
    const events = [
      event({ id: 1, event_type: 'tool_use', payload: { id: 't1', name: 'Bash', input: 'true' } }),
      event({ id: 2, event_type: 'tool_result', payload: { tool_use_id: 't1', content: '' } }),
    ];
    render(
      <Harness
        events={events}
        initial={{ kind: 'step', stepKey: toolKeyOf(events), tab: 'result' }}
      />,
    );

    expect(screen.getByText('Tool completed with no output.')).toBeInTheDocument();
  });

  it('reports a stored payload as truncated instead of implying it is whole', () => {
    const events = [
      event({
        id: 1,
        event_type: 'tool_result',
        payload: {
          tool_use_id: 't1',
          content: 'head',
          truncated: true,
          original_payload_bytes: 900000,
        },
      }),
    ];
    render(
      <Harness
        events={events}
        initial={{ kind: 'step', stepKey: toolKeyOf(events), tab: 'result' }}
      />,
    );

    expect(screen.getByText(/Truncated by the platform/)).toBeInTheDocument();
  });

  it('says when the current search hides the selected step', () => {
    const key = toolKeyOf(ASSISTANT_WITH_TOOL);
    const onClearQuery = vi.fn();
    render(
      <Harness
        events={ASSISTANT_WITH_TOOL}
        initial={{ kind: 'step', stepKey: key, tab: 'summary' }}
        query="nothing-matches-this"
        onClearQuery={onClearQuery}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));

    expect(onClearQuery).toHaveBeenCalled();
  });

  it('keeps unknown event types readable with a Summary and a Raw view', () => {
    const events = [event({ id: 1, event_type: 'mystery', payload: { x: 1 } })];
    const key = buildTrajectorySteps(events)[0].key;
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'summary' }} />);

    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Summary', 'Raw']);
    expect(screen.getByText(/Event · Turn 7/)).toBeInTheDocument();
  });

  it('reads the harness a lifecycle event reported', () => {
    const events = [
      event({
        id: 1,
        event_type: 'lifecycle',
        payload: {
          phase: 'init',
          model: 'glm-5.1',
          runtime_version: '2.1.220',
          harness: {
            tools: ['Bash', 'Read', 'mcp__relay__search'],
            mcp_servers: [{ name: 'relay', status: 'connected' }],
            permission_mode: 'bypassPermissions',
          },
        },
      }),
    ];
    const key = buildTrajectorySteps(events)[0].key;
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'summary' }} />);

    // The two views exist only because this event carries a report to read.
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      'Summary',
      'System Prompt',
      'Tools',
      'Raw',
    ]);

    fireEvent.click(screen.getByRole('tab', { name: 'Tools' }));
    expect(screen.getByText('Bash')).toBeInTheDocument();
    expect(screen.getByText('mcp__relay__search')).toBeInTheDocument();
    expect(screen.getByText('connected')).toBeInTheDocument();
    expect(screen.getByText(/does not report what each tool accepts/)).toBeInTheDocument();
  });

  it('says the runtime reported no system prompt rather than showing one', () => {
    const events = [
      event({
        id: 1,
        event_type: 'lifecycle',
        payload: {
          phase: 'init',
          model: 'glm-5.1',
          runtime_version: '2.1.220',
          harness: { tools: ['Bash'], permission_mode: 'bypassPermissions' },
        },
      }),
    ];
    const key = buildTrajectorySteps(events)[0].key;
    render(
      <Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'systemPrompt' }} />,
    );

    expect(screen.getByText(/builds its system prompt inside the CLI/)).toBeInTheDocument();
    expect(screen.getByText('2.1.220')).toBeInTheDocument();
    expect(screen.getByText('bypassPermissions')).toBeInTheDocument();
  });

  it('says when the platform kept only part of the reported tool list', () => {
    const events = [
      event({
        id: 1,
        event_type: 'lifecycle',
        payload: { phase: 'init', harness: { tools: ['Bash'], tools_truncated: true } },
      }),
    ];
    const key = buildTrajectorySteps(events)[0].key;
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'tools' }} />);

    expect(screen.getByText(/stored the first 1 tool name\./)).toBeInTheDocument();
  });

  it('opens no harness view for a report the platform truncated to text', () => {
    const events = [
      event({
        id: 1,
        event_type: 'lifecycle',
        payload: { phase: 'init', harness: '{"tools": ["Bash"]}', truncated: true },
      }),
    ];
    const key = buildTrajectorySteps(events)[0].key;
    render(<Harness events={events} initial={{ kind: 'step', stepKey: key, tab: 'summary' }} />);

    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['Summary', 'Raw']);
  });
});
