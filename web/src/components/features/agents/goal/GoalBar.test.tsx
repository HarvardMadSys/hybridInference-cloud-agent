// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { GoalBar } from './GoalBar';
import { hasActiveGoal, hasLiveWork } from './liveWork';
import type { AgentGoal } from '../types';

// The band and the menu are rendered directly rather than through JobDetail:
// what they say is the whole subject here, and mounting the page would bury
// each assertion under a transcript.

function goal(overrides: Partial<AgentGoal> = {}): AgentGoal {
  return {
    objective: 'make `pytest` pass',
    state: 'active',
    revision: 3,
    roundsUsed: 2,
    maxRounds: 10,
    ...overrides,
  };
}

const idle = { live: false, repairing: false };

afterEach(cleanup);

describe('GoalBar', () => {
  it('shows progress while the goal is running itself', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal running · 2/10 rounds · no round in flight',
    );
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument();
  });

  it('shows the objective it is working toward, not just its state', () => {
    // A status line alone leaves the owner to guess which goal it describes.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ objective: 'ship the release notes' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('ship the release notes');
  });

  it('says a round is in flight rather than looking idle', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal()}
        state={{ live: true, repairing: false }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Goal in progress · 2/10 rounds');
  });

  it('names a repair round as its own state, not as more work', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal()}
        state={{ live: true, repairing: true }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Restating the completion report · 2/10 rounds',
    );
  });

  it('carries the reason a paused goal stopped, and the sentence behind it', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'context_lost',
          pauseDetail: "the conversation's session is gone",
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      "Goal paused · the sandbox context was lost · the conversation's session is gone",
    );
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });

  it('says a fork will not continue on its own, whatever else it says', () => {
    // The pause reason is `fork`, and the owner needs the consequence rather
    // than the cause: "paused" does not tell them this one never resumes
    // itself.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'fork',
          pauseDetail: 'copied from another conversation; resume it to start working',
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Forked conversation — the goal will not continue on its own',
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'copied from another conversation; resume it to start working',
    );
  });

  it('separates running out of rounds from a report it could not read', () => {
    const { rerender } = render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'round_limit',
          pauseDetail: 'the goal used its whole automatic round budget',
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal paused · all rounds used · the goal used its whole automatic round budget',
    );
    rerender(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'report_invalid',
          pauseDetail: 'the goal report could not be read (malformed_json)',
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal paused · the completion report could not be read · the goal report could not be read (malformed_json)',
    );
  });

  it('says pausing leaves the round in flight alone, and points at Stop', () => {
    // "Stop after this turn" and "stop this turn" are different requests. The
    // band has to say which one Pause made while a round is still running.
    const paused = goal({
      state: 'paused',
      pauseReason: 'user',
      pauseDetail: 'paused by the user',
    });
    const { rerender } = render(
      <GoalBar threadId="thread_1" goal={paused} state={idle} onChanged={() => {}} />,
    );
    expect(screen.getByRole('status')).not.toHaveTextContent('use Stop');

    rerender(
      <GoalBar
        threadId="thread_1"
        goal={paused}
        state={{ live: true, repairing: false }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'The round already running will finish; use Stop to end it now.',
    );
  });

  it('offers nothing but clearing once the goal is done', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ state: 'complete', completionBasis: 'model_reported' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal complete · reported by the agent · 2/10 rounds',
    );
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
  });

  it('claims no basis for a completion that did not record one', () => {
    // Only `model_reported` exists today. A row that carries something else is
    // not evidence that the agent reported it, and the band must not say so.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ state: 'complete' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Goal complete · 2/10 rounds');
    expect(screen.getByRole('status')).not.toHaveTextContent('reported by the agent');
  });
});

describe('hasLiveWork', () => {
  it('counts a queued turn behind the one being viewed', () => {
    // The page can be showing a finished turn while a later one waits. A band
    // that read only the viewed job would look idle with work in the queue.
    expect(hasLiveWork({ state: 'done', conversationState: 'queued' })).toBe(true);
    expect(hasLiveWork({ state: 'done', conversationState: 'idle' })).toBe(false);
    expect(hasLiveWork({ state: 'running' })).toBe(true);
  });
});

describe('hasActiveGoal', () => {
  it('keeps observing a goal that owes the conversation another round', () => {
    // The previous round settles and the next one is queued a moment later, so
    // an active goal is legitimately idle in between.
    expect(hasActiveGoal({ state: 'active' })).toBe(true);
    expect(hasActiveGoal({ state: 'paused' })).toBe(false);
    expect(hasActiveGoal({ state: 'complete' })).toBe(false);
    expect(hasActiveGoal(undefined)).toBe(false);
  });
});
