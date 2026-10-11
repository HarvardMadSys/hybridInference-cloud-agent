// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LiveAgentStatus } from './LiveAgentStatus';
import type { AgentJob } from './types';

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'job',
    title: 'A turn',
    state: 'running',
    currentAttemptId: 2,
    repo: 'o/r',
    baseSha: 'abc1234',
    branch: 'agent/job',
    runtime: 'codex',
    model: 'model',
    spentUsd: 0,
    timeoutLabel: '',
    sandbox: '',
    attempts: [],
    events: [],
    diffFiles: [],
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 0 },
    liveNote: 'Waiting for LLM response',
    liveNoteStartedAt: '2026-10-10T12:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime('2026-10-10T12:00:58Z');
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LiveAgentStatus', () => {
  it('retains the complete phase text and exposes it in the status and title', () => {
    const liveNote = 'Running a tool call while checking a particularly long repository path';
    render(<LiveAgentStatus job={job({ liveNote })} />);

    const status = screen.getByRole('status', { name: 'Agent status' });
    expect(status).toHaveTextContent(liveNote);
    expect(status).toHaveAttribute('title', liveNote);
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveAttribute('aria-atomic', 'true');
  });

  it.each(['Waiting for LLM response', 'Running tool call', 'Receiving LLM response'])(
    'shows an updating timer beside %s using its recorded start',
    (liveNote) => {
      render(<LiveAgentStatus job={job({ liveNote })} />);
      const status = screen.getByRole('status', { name: 'Agent status' });
      const timer = screen.getByRole('timer', { name: 'Elapsed activity time' });
      expect(status).toHaveTextContent(liveNote);
      expect(timer).toHaveTextContent(/^58s$/);
      expect(status.parentElement).toBe(timer.parentElement);
      // The clock is readable without announcing every tick as a status update.
      expect(timer).toHaveAttribute('aria-live', 'off');

      act(() => vi.advanceTimersByTime(3000));
      expect(timer).toHaveTextContent(/^1m 1s$/);
    },
  );

  it('keeps elapsed time across ordinary job updates and a remount', () => {
    const current = job();
    const { rerender, unmount } = render(<LiveAgentStatus job={current} />);
    act(() => vi.advanceTimersByTime(2000));
    rerender(<LiveAgentStatus job={{ ...current, spentUsd: 0.1 }} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^1m 0s$/);

    unmount();
    render(<LiveAgentStatus job={current} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^1m 0s$/);
  });

  it('restarts timing when a tool starts and when model waiting resumes', () => {
    const { rerender } = render(<LiveAgentStatus job={job()} />);
    rerender(
      <LiveAgentStatus
        job={job({
          liveNote: 'Running tool call',
          liveNoteStartedAt: '2026-10-10T12:00:58Z',
        })}
      />,
    );
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
    act(() => vi.advanceTimersByTime(4000));
    expect(screen.getByRole('timer')).toHaveTextContent(/^4s$/);

    rerender(<LiveAgentStatus job={job({ liveNoteStartedAt: '2026-10-10T12:01:02Z' })} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
  });

  it.each([undefined, 'invalid'])('times an undated activity locally for %s', (startedAt) => {
    const current = job({ liveNoteStartedAt: startedAt });
    const { rerender } = render(<LiveAgentStatus job={current} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
    act(() => vi.advanceTimersByTime(2000));
    rerender(<LiveAgentStatus job={{ ...current, spentUsd: 0.1 }} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^2s$/);

    rerender(<LiveAgentStatus job={{ ...current, currentAttemptId: 3 }} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
    act(() => vi.advanceTimersByTime(1000));
    rerender(<LiveAgentStatus job={{ ...current, id: 'next-job' }} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
  });

  it('never shows negative elapsed time for a future timestamp', () => {
    render(<LiveAgentStatus job={job({ liveNoteStartedAt: '2026-10-10T12:01:00Z' })} />);
    expect(screen.getByRole('timer')).toHaveTextContent(/^0s$/);
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.getByRole('timer')).toHaveTextContent(/^1s$/);
  });

  it.each(['done', 'cancelled', 'failed', 'queued'] as const)(
    'removes the timer and its interval when the job becomes %s',
    (state) => {
      const { rerender } = render(<LiveAgentStatus job={job()} />);
      rerender(<LiveAgentStatus job={job({ state })} />);
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
      expect(screen.queryByRole('timer')).not.toBeInTheDocument();
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it('removes the timer when live activity settles, even before the job state changes', () => {
    const { rerender } = render(<LiveAgentStatus job={job()} />);
    rerender(<LiveAgentStatus job={job({ liveNote: undefined })} />);
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });
});
