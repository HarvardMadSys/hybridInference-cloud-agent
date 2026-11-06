// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJob,
  getAgentAttemptTelemetry,
  getAgentTrajectoryTimings,
  type AgentJobApi,
  type AgentAttemptTelemetryApi,
} from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';
import { TrajectoryTimeBreakdown } from './TrajectoryTimeBreakdown';

afterEach(cleanup);
vi.mock('@/lib/api/agents', () => ({
  AGENT_TRAJECTORY_TIMING_BATCH_SIZE: 100,
  getAgentJob: vi.fn(),
  getAgentAttemptTelemetry: vi.fn(),
  getAgentTrajectoryTimings: vi.fn(),
}));
beforeEach(() => {
  vi.mocked(getAgentTrajectoryTimings)
    .mockReset()
    .mockRejectedValue(new APIError('UNKNOWN_ERROR', 'Not found', 405));
  vi.mocked(getAgentJob).mockReset().mockRejectedValue(new Error('Unavailable'));
  vi.mocked(getAgentAttemptTelemetry).mockReset().mockRejectedValue(new Error('Unavailable'));
});

function job(overrides: Partial<AgentJobApi> = {}): AgentJobApi {
  return {
    id: 'job-1',
    turn_no: 1,
    telemetry_attempt_id: 2,
    duration_ms: 60_000,
    attempt_telemetry: [
      {
        attempt_id: 2,
        attempt_no: 1,
        duration_ms: 60_000,
        continuity_mode: null,
        context_status: null,
      },
    ],
    resources: {
      tool_time_ms: 15_000,
      tool_calls: [
        { tool_use_id: 'a', timing_source: 'exact', duration_ms: 10_000 },
        { tool_use_id: 'b', timing_source: 'exact', duration_ms: 10_000 },
      ],
    },
    ...overrides,
  } as AgentJobApi;
}

function value(label: string): string | null {
  return screen.getByText(label).parentElement!.querySelector('dd')!.textContent;
}

describe('TrajectoryTimeBreakdown', () => {
  it('loads one hundred three-attempt turns with one compact request', async () => {
    const jobs = Array.from({ length: 100 }, (_, index) =>
      job({
        id: `job-${index + 1}`,
        turn_no: index + 1,
        state: 'succeeded',
        revision: 1,
        current_attempt_id: index * 3 + 3,
        telemetry_attempt_id: null,
        duration_ms: null,
        attempt_telemetry: [],
        resources: null,
      }),
    );
    vi.mocked(getAgentTrajectoryTimings).mockResolvedValue({
      jobs: jobs.map((item, jobIndex) => ({
        job_id: item.id,
        attempts: Array.from({ length: 3 }, (_, attemptIndex) => ({
          attempt_id: jobIndex * 3 + attemptIndex + 1,
          attempt_no: attemptIndex + 1,
          continuity_mode: null,
          context_status: null,
          duration_ms: 1_000,
          tool_time_ms: 100,
          tool_calls_truncated: false,
          tool_timing_exact: true,
        })),
      })),
    });

    render(<TrajectoryTimeBreakdown jobs={jobs} />);

    await waitFor(() => expect(value('Recorded execution')).toBe('5m'));
    expect(value('Using tools')).toBe('30s');
    expect(value('Other work')).toBe('4m 30s');
    expect(getAgentTrajectoryTimings).toHaveBeenCalledOnce();
    expect(getAgentTrajectoryTimings).toHaveBeenCalledWith(
      jobs.map((item) => item.id),
      expect.any(AbortSignal),
    );
    expect(getAgentJob).not.toHaveBeenCalled();
    expect(getAgentAttemptTelemetry).not.toHaveBeenCalled();
  });

  it('preserves compact message-timing and truncation semantics', async () => {
    const first = job({ resources: null });
    const second = job({ id: 'job-2', turn_no: 2, resources: null });
    vi.mocked(getAgentTrajectoryTimings).mockResolvedValue({
      jobs: [
        {
          job_id: first.id,
          attempts: [
            {
              ...first.attempt_telemetry![0],
              tool_time_ms: 15_000,
              tool_calls_truncated: false,
              tool_timing_exact: false,
            },
          ],
        },
        {
          job_id: second.id,
          attempts: [
            {
              ...second.attempt_telemetry![0],
              tool_time_ms: 15_000,
              tool_calls_truncated: true,
              tool_timing_exact: true,
            },
          ],
        },
      ],
    });

    render(<TrajectoryTimeBreakdown jobs={[first, second]} />);

    await waitFor(() => expect(value('Using tools')).toBe('≥ ≈ 15s'));
    expect(value('Recorded execution')).toBe('2m');
    expect(value('Other work')).toBe('—');
  });

  it('caps legacy detail requests when the bulk route is unavailable', async () => {
    const jobs = Array.from({ length: 12 }, (_, index) =>
      job({ id: `job-${index + 1}`, turn_no: index + 1, resources: null }),
    );
    let active = 0;
    let maxActive = 0;
    vi.mocked(getAgentAttemptTelemetry).mockImplementation(async (_jobId, attemptId) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return {
        ...jobs[0].attempt_telemetry![0],
        attempt_id: attemptId,
        resources: jobs[0].resources,
      } as AgentAttemptTelemetryApi;
    });

    render(<TrajectoryTimeBreakdown jobs={jobs} />);

    await waitFor(() => expect(getAgentAttemptTelemetry).toHaveBeenCalledTimes(12));
    await waitFor(() => expect(active).toBe(0));
    expect(maxActive).toBe(4);
    expect(getAgentJob).not.toHaveBeenCalled();
  });

  it('loads historical summaries and resource timing omitted from the conversation API', async () => {
    const historical = job();
    vi.mocked(getAgentJob).mockResolvedValue({ ...historical, resources: null });
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
      ...historical.attempt_telemetry![0],
      resources: historical.resources,
    } as AgentAttemptTelemetryApi);
    render(
      <TrajectoryTimeBreakdown
        jobs={[
          { ...historical, attempt_telemetry: [], resources: null, telemetry_attempt_id: null },
        ]}
      />,
    );
    await waitFor(() => expect(value('Other work')).toBe('45s'));
    expect(getAgentJob).toHaveBeenCalledWith('job-1');
    expect(getAgentAttemptTelemetry).toHaveBeenCalledWith('job-1', 2, expect.any(AbortSignal));
  });

  it('loads retry timing within its own attempt', async () => {
    const retry = job();
    retry.attempt_telemetry!.unshift({
      attempt_id: 1,
      attempt_no: 0,
      duration_ms: 30_000,
      continuity_mode: null,
      context_status: null,
    });
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue({
      ...retry.attempt_telemetry![0],
      resources: { ...retry.resources, tool_time_ms: 5_000 },
    } as AgentAttemptTelemetryApi);
    render(<TrajectoryTimeBreakdown jobs={[retry]} />);
    await waitFor(() => expect(value('Using tools')).toBe('20s'));
    expect(value('Recorded execution')).toBe('1m 30s');
    expect(value('Other work')).toBe('1m 10s');
    expect(getAgentAttemptTelemetry).toHaveBeenCalledWith('job-1', 1, expect.any(AbortSignal));
  });
  it('uses the recorded union of overlapping calls and excludes gaps between turns', () => {
    render(<TrajectoryTimeBreakdown jobs={[job(), job({ id: 'job-2', turn_no: 2 })]} />);
    expect(value('Recorded execution')).toBe('2m');
    expect(value('Using tools')).toBe('30s');
    expect(value('Other work')).toBe('1m 30s');
    expect(screen.getByText('By turn and attempt (2)')).toBeInTheDocument();
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(3);
  });

  it('includes retry execution without assigning selected resources to the earlier attempt', () => {
    const retry = job();
    retry.attempt_telemetry!.unshift({
      attempt_id: 1,
      attempt_no: 0,
      duration_ms: 30_000,
      continuity_mode: null,
      context_status: null,
    });
    render(<TrajectoryTimeBreakdown jobs={[retry]} />);
    expect(value('Recorded execution')).toBe('1m 30s');
    expect(value('Using tools')).toBe('≥ 15s');
    expect(value('Other work')).toBe('—');
    const row = screen.getByRole('rowheader', { name: '1 / 0' }).parentElement!;
    expect(within(row).getAllByText('—')).toHaveLength(2);
  });

  it('marks timing derived from messages as approximate', () => {
    const approximate = job();
    approximate.resources!.tool_calls![0].timing_source = 'message';
    render(<TrajectoryTimeBreakdown jobs={[approximate]} />);
    expect(value('Using tools')).toBe('≈ 15s');
    expect(value('Other work')).toBe('≈ 45s');
  });

  it('does not treat absent or truncated tool measurements as zero', () => {
    const truncated = job();
    truncated.resources!.tool_calls_truncated = true;
    render(<TrajectoryTimeBreakdown jobs={[truncated, job({ resources: null })]} />);
    expect(value('Using tools')).toBe('—');
    expect(value('Other work')).toBe('—');
  });

  it('preserves measured zero and rejects an impossible remainder', () => {
    const zero = job();
    zero.resources!.tool_time_ms = 0;
    const { rerender } = render(<TrajectoryTimeBreakdown jobs={[zero]} />);
    expect(value('Using tools')).toBe('0ms');
    expect(value('Other work')).toBe('1m');
    zero.resources!.tool_time_ms = 70_000;
    rerender(<TrajectoryTimeBreakdown jobs={[{ ...zero }]} />);
    expect(value('Other work')).toBe('—');
  });

  it('shows an unavailable state before execution timing arrives', () => {
    render(<TrajectoryTimeBreakdown jobs={[]} />);
    expect(screen.getByText('Timing not recorded yet.')).toBeInTheDocument();
  });

  it('keeps a historical turn with missing timing in the coverage of the total', async () => {
    render(
      <TrajectoryTimeBreakdown
        jobs={[
          job(),
          job({
            id: 'missing',
            state: 'succeeded',
            attempt_telemetry: [],
            telemetry_attempt_id: null,
            resources: null,
          }),
        ]}
      />,
    );
    await waitFor(() => expect(getAgentJob).toHaveBeenCalledWith('missing'));
    expect(value('Recorded execution')).toBe('≥ 1m');
    expect(value('Using tools')).toBe('≥ 15s');
    expect(value('Other work')).toBe('—');
  });
});
