// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal, setAgentGoal } from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';

import { GoalBar } from './GoalBar';
import { hasActiveGoal, hasLiveWork } from './liveWork';
import type { AgentGoal } from '../types';

vi.mock('@/lib/api/agents', () => ({
  getAgentGoal: vi.fn(),
  setAgentGoal: vi.fn(),
  pauseAgentGoal: vi.fn(),
  resumeAgentGoal: vi.fn(),
  clearAgentGoal: vi.fn(),
}));

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

beforeEach(() => {
  vi.mocked(pauseAgentGoal).mockReset();
  vi.mocked(resumeAgentGoal).mockReset();
  vi.mocked(clearAgentGoal).mockReset();
  vi.mocked(setAgentGoal).mockReset();
  vi.mocked(pauseAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 4 });
  vi.mocked(resumeAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 4 });
  vi.mocked(clearAgentGoal).mockResolvedValue({
    thread_id: 'thread_1',
    cleared: true,
    revision: 5,
  });
  vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 4 });
});

afterEach(cleanup);

describe('GoalBar', () => {
  it('names the band, so it is not one of two anonymous status regions', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('status', { name: 'Completion condition' })).toBeInTheDocument();
  });

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

// The band's controls are the whole of the goal's management surface, so what
// each one does — and, more importantly, what it does not do — is behaviour
// worth pinning down.
describe('GoalBar controls', () => {
  it('names every control and says what it does', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    const hints: [string, RegExp][] = [
      ['Pause', /round in flight/],
      ['Edit', /round in flight is left alone/],
      ['Clear', /chat and its history stay/],
    ];
    for (const [name, hint] of hints) {
      const control = screen.getByRole('button', { name });
      expect(control.getAttribute('title')).toMatch(hint);
    }
  });

  it('pauses the goal without stopping the round already running', async () => {
    const onChanged = vi.fn();
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal()}
        state={{ live: true, repairing: false }}
        onChanged={onChanged}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(pauseAgentGoal).toHaveBeenCalledWith('thread_1'));
    expect(onChanged).toHaveBeenCalled();
  });

  it('edits a running goal in place, without pausing or interrupting it', async () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    // The revision the band was drawn from travels with the write.
    await waitFor(() =>
      expect(setAgentGoal).toHaveBeenCalledWith('thread_1', {
        goal: { objective: 'ship the release notes', max_rounds: 10 },
        revision: 3,
      }),
    );
    expect(pauseAgentGoal).not.toHaveBeenCalled();
  });

  it('leaves a paused goal paused when its objective is edited', async () => {
    // Changing the words is not resuming it: the owner stopped this goal, and
    // only the owner starts it again.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ state: 'paused', pauseReason: 'user', pauseDetail: 'paused by the user' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    await waitFor(() => expect(setAgentGoal).toHaveBeenCalledTimes(1));
    expect(resumeAgentGoal).not.toHaveBeenCalled();
  });

  it('clears a goal that is still running, and nothing else', async () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal()}
        state={{ live: true, repairing: false }}
        onChanged={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(clearAgentGoal).toHaveBeenCalledWith('thread_1'));
    expect(pauseAgentGoal).not.toHaveBeenCalled();
  });

  it('refuses to resume a goal that has spent its budget, and says what to do', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'round_limit',
          pauseDetail: 'the goal used its whole automatic round budget',
          roundsUsed: 10,
          maxRounds: 10,
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent(
      'All 10 automatic rounds are used. Raise the limit to continue.',
    );
    // The way out is the control beside it, which keeps the goal paused and
    // leaves the rounds already spent alone.
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(resumeAgentGoal).not.toHaveBeenCalled();
  });

  it('keeps a refused write on screen and does not pretend it landed', async () => {
    const onChanged = vi.fn();
    vi.mocked(pauseAgentGoal).mockRejectedValue(new Error('goal service unavailable'));
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('goal service unavailable');
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('reads the goal back after a version conflict instead of overwriting', async () => {
    const onChanged = vi.fn();
    vi.mocked(setAgentGoal).mockRejectedValue(
      new APIError('goal_revision_conflict', 'This goal changed while you were editing it.'),
    );
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={onChanged} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    // The revision this band holds is exactly what the server refused, so the
    // goal has to be re-read before the owner can try again.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This goal changed while you were editing it.',
    );
  });

  it('lets a resume through once the round limit has been raised', async () => {
    // `round_limit` is the history of why the goal stopped, not a standing
    // verdict. Raising the cap leaves it on the row, and a band that refused on
    // the reason alone would keep Resume disabled for a goal the server would
    // now happily continue.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'round_limit',
          pauseDetail: 'the goal used its whole automatic round budget',
          roundsUsed: 1,
          maxRounds: 2,
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).not.toHaveTextContent('Raise the limit to continue.');
    const resume = screen.getByRole('button', { name: 'Resume' });
    expect(resume).toBeEnabled();
    fireEvent.click(resume);
    await waitFor(() => expect(resumeAgentGoal).toHaveBeenCalledWith('thread_1'));
  });

  it('keeps the dialog open, and the words in it, when an edit is refused', async () => {
    vi.mocked(setAgentGoal).mockRejectedValueOnce(new Error('goal service unavailable'));
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));

    // Still open, still holding what was typed, and saying why: closing on the
    // way out would throw away the retry.
    expect(await screen.findByRole('alert')).toHaveTextContent('goal service unavailable');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');

    vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 5 });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps the draft through the re-read a conflict forces', async () => {
    // A 409 makes the page re-read the goal, and the re-read moves `initial`
    // under the open dialog. Typing the other tab's goal over this owner's
    // words would throw away the retry the open dialog exists to allow.
    vi.mocked(setAgentGoal).mockRejectedValueOnce(
      new APIError('goal_revision_conflict', 'This goal changed while you were editing it.'),
    );
    const { rerender } = render(
      <GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.change(screen.getByLabelText('Maximum automatic rounds'), {
      target: { value: '15' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    await waitFor(() => expect(setAgentGoal).toHaveBeenCalledTimes(1));

    // What the other tab wrote, arriving as the re-read the refusal caused.
    rerender(
      <GoalBar
        threadId="thread_1"
        goal={goal({ objective: 'the other tab’s objective', revision: 4, maxRounds: 20 })}
        state={idle}
        onChanged={() => {}}
      />,
    );

    expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');
    expect(screen.getByLabelText('Maximum automatic rounds')).toHaveValue(15);

    // And the retry goes out as written, against the revision the re-read brought.
    vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 5 });
    fireEvent.click(screen.getByRole('button', { name: 'Set condition' }));
    await waitFor(() =>
      expect(setAgentGoal).toHaveBeenLastCalledWith('thread_1', {
        goal: { objective: 'ship the release notes', max_rounds: 15 },
        revision: 4,
      }),
    );
  });

  it('shows the whole objective on demand, however long it is', () => {
    const objective =
      'make every unit test pass, then update the changelog, then open a draft pull request against dev and leave the tree clean';
    render(
      <GoalBar threadId="thread_1" goal={goal({ objective })} state={idle} onChanged={() => {}} />,
    );
    const shown = screen.getByRole('button', { name: objective });
    // Truncated on the band, whole in the tooltip and one click away.
    expect(shown.getAttribute('title')).toBe(objective);
    expect(shown).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(shown);
    expect(shown).toHaveAttribute('aria-expanded', 'true');
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
