// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { GoalBar } from './GoalBar';
import { hasLiveWork } from './liveWork';
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
    blockedStreak: 0,
    ...overrides,
  };
}

const idle = { live: false, verifying: false };

afterEach(cleanup);

describe('GoalBar', () => {
  it('shows progress while the goal is running itself', () => {
    render(<GoalBar jobId="ajob_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal running · 2/10 rounds · no round in flight',
    );
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument();
  });

  it('says a round is in flight rather than looking idle', () => {
    render(
      <GoalBar
        jobId="ajob_1"
        goal={goal()}
        state={{ live: true, verifying: false }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Goal in progress · 2/10 rounds');
  });

  it('names verification as its own state, not as more work', () => {
    render(
      <GoalBar
        jobId="ajob_1"
        goal={goal()}
        state={{ live: true, verifying: true }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Verifying completion · 2/10 rounds');
  });

  it('carries the reason a paused goal stopped', () => {
    render(
      <GoalBar
        jobId="ajob_1"
        goal={goal({ state: 'paused', pauseReason: 'context-lost' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal paused · the sandbox context was lost',
    );
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });

  it('says a fork will not continue on its own, whatever else it says', () => {
    // The pause reason is `fork`, and the owner needs the consequence rather
    // than the cause: "paused" does not tell them this one never resumes
    // itself.
    render(
      <GoalBar
        jobId="ajob_1"
        goal={goal({ state: 'paused', pauseReason: 'fork' })}
        state={{ ...idle, forked: true }}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Forked conversation — the goal will not continue on its own',
    );
  });

  it('separates running out of rounds from the model giving up', () => {
    const { rerender } = render(
      <GoalBar
        jobId="ajob_1"
        goal={goal({ state: 'blocked', blockedCode: 'round-limit' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Goal blocked · all rounds used');
    rerender(
      <GoalBar
        jobId="ajob_1"
        goal={goal({
          state: 'blocked',
          blockedCode: 'model-reported',
          blockedMessage: 'no network',
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'Goal blocked · the model reports it cannot continue · no network',
    );
  });

  it('offers nothing but clearing once the goal is done', () => {
    render(
      <GoalBar
        jobId="ajob_1"
        goal={goal({ state: 'complete' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Goal complete · 2/10 rounds');
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
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
