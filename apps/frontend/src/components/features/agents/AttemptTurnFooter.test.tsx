// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAgentAttemptTelemetry, type AgentAttemptTelemetryApi } from '@/lib/api/agents';

import { AttemptTurnFooter } from './AttemptTurnFooter';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({ getAgentAttemptTelemetry: vi.fn() }));
vi.mock('./TurnFooter', () => ({
  TurnFooter: ({ job, attemptNo }: { job: AgentJob; attemptNo: number }) => (
    <output data-testid="telemetry">
      {JSON.stringify(job.attempts.find((a) => a.no === attemptNo)?.telemetry ?? null)}
    </output>
  ),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const job = {
  id: 'ajob_1',
  state: 'done',
  attemptTelemetryAvailable: true,
  attempts: [
    { id: 10, no: 1, status: 'superseded' },
    { id: 11, no: 2, status: 'finished' },
  ],
  durationMs: 999_000,
} as AgentJob;

function telemetry(attemptId: number): AgentAttemptTelemetryApi {
  return {
    attempt_id: attemptId,
    attempt_no: attemptId - 9,
    continuity_mode: null,
    context_status: null,
    duration_ms: attemptId * 100,
    resources: null,
  };
}

describe('AttemptTurnFooter', () => {
  it('loads only the selected attempt, without reloading on unchanged job polls', async () => {
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue(telemetry(10));
    const { rerender } = render(<AttemptTurnFooter job={job} attemptNo={1} />);
    await waitFor(() => expect(screen.getByTestId('telemetry')).toHaveTextContent('1000'));
    expect(getAgentAttemptTelemetry).toHaveBeenCalledWith('ajob_1', 10, expect.any(AbortSignal));
    rerender(<AttemptTurnFooter job={{ ...job, attempts: [...job.attempts] }} attemptNo={1} />);
    expect(getAgentAttemptTelemetry).toHaveBeenCalledTimes(1);
  });

  it('clears old detail immediately and ignores late results after switching attempts', async () => {
    let finishOld!: (value: AgentAttemptTelemetryApi) => void;
    vi.mocked(getAgentAttemptTelemetry).mockImplementation((_job, id) => {
      if (id === 10)
        return new Promise((resolve) => {
          finishOld = resolve;
        });
      return Promise.resolve(telemetry(11));
    });
    const { rerender } = render(<AttemptTurnFooter job={job} attemptNo={1} />);
    const oldSignal = vi.mocked(getAgentAttemptTelemetry).mock.calls[0][2]!;
    rerender(<AttemptTurnFooter job={job} attemptNo={2} />);
    expect(oldSignal.aborted).toBe(true);
    expect(screen.getByTestId('telemetry')).toHaveTextContent('null');
    await waitFor(() => expect(screen.getByTestId('telemetry')).toHaveTextContent('1100'));
    await act(async () => {
      finishOld(telemetry(10));
    });
    expect(screen.getByTestId('telemetry')).toHaveTextContent('1100');
    expect(screen.getByTestId('telemetry')).not.toHaveTextContent('1000');
  });

  it('does not show loaded detail from the previous selection while the next loads', async () => {
    vi.mocked(getAgentAttemptTelemetry)
      .mockResolvedValueOnce(telemetry(10))
      .mockImplementation(() => new Promise(() => {}));
    const { rerender } = render(<AttemptTurnFooter job={job} attemptNo={1} />);
    await waitFor(() => expect(screen.getByTestId('telemetry')).toHaveTextContent('1000'));
    rerender(<AttemptTurnFooter job={job} attemptNo={2} />);
    expect(screen.getByTestId('telemetry')).toHaveTextContent('null');
  });

  it('leaves errors unmeasured, supports retry, and rejects mismatched identities', async () => {
    vi.mocked(getAgentAttemptTelemetry)
      .mockResolvedValueOnce(telemetry(11))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(telemetry(10));
    render(<AttemptTurnFooter job={job} attemptNo={1} />);
    await screen.findByText(/Attempt measurements unavailable/);
    expect(screen.getByTestId('telemetry')).toHaveTextContent('null');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText(/Attempt measurements unavailable/);
    expect(screen.getByTestId('telemetry')).not.toHaveTextContent('999000');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByTestId('telemetry')).toHaveTextContent('1000'));
  });

  it("refreshes on completion and cannot retain another job's detail", async () => {
    vi.mocked(getAgentAttemptTelemetry).mockResolvedValue(telemetry(10));
    const { rerender } = render(
      <AttemptTurnFooter job={{ ...job, state: 'running' }} attemptNo={1} />,
    );
    await waitFor(() => expect(screen.getByTestId('telemetry')).toHaveTextContent('1000'));
    rerender(<AttemptTurnFooter job={job} attemptNo={1} />);
    await waitFor(() => expect(getAgentAttemptTelemetry).toHaveBeenCalledTimes(2));
    vi.mocked(getAgentAttemptTelemetry).mockImplementation(() => new Promise(() => {}));
    rerender(<AttemptTurnFooter job={{ ...job, id: 'ajob_2' }} attemptNo={1} />);
    expect(screen.getByTestId('telemetry')).toHaveTextContent('null');
  });

  it('does not call the new endpoint for a legacy server', () => {
    render(<AttemptTurnFooter job={{ ...job, attemptTelemetryAvailable: false }} attemptNo={1} />);
    expect(getAgentAttemptTelemetry).not.toHaveBeenCalled();
  });
});
