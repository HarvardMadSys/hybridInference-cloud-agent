// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal, setAgentGoal } from '@/lib/api/agents';
import { onPlatform } from '@/lib/platformTestSupport';
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

// The band is rendered directly rather than through JobDetail: what it says and
// what its controls do is the whole subject here, and mounting the page would
// bury each assertion under a transcript.

function goal(overrides: Partial<AgentGoal> = {}): AgentGoal {
  return {
    objective: 'make `pytest` pass',
    state: 'active',
    revision: 3,
    roundsUsed: 2,
    // No bound, which is what the product creates: an objective and nothing
    // else. Tests about a bound the owner stated one themselves.
    maxRounds: null,
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
    expect(screen.getByRole('status', { name: 'Goal' })).toBeInTheDocument();
  });

  it('counts the rounds that ran, and names no total for a goal without one', () => {
    // "2/10" reported a limit nobody had set, and made "no limit" — the
    // ordinary case — impossible to say.
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for next round');
    expect(screen.getByRole('status')).toHaveTextContent('2 rounds run');
    expect(screen.getByRole('status')).not.toHaveTextContent('/');
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument();
  });

  it('names the total when, and only when, the owner set one', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ maxRounds: 10 })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('2 of at most 10 rounds');
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
    expect(screen.getByRole('button', { name: 'ship the release notes' })).toBeInTheDocument();
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
    expect(screen.getByRole('status')).toHaveTextContent('In progress');
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
    expect(screen.getByRole('status')).toHaveTextContent('Updating report');
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
      "the sandbox context was lost · the conversation's session is gone",
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

  it('separates reaching a stated bound from a report it could not read', () => {
    const { rerender } = render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'round_limit',
          pauseDetail: 'the goal reached the round limit its owner set',
          roundsUsed: 10,
          maxRounds: 10,
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent(
      'all rounds used · the goal reached the round limit its owner set',
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
      'the completion report could not be read · the goal report could not be read (malformed_json)',
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
    expect(screen.queryByText(/use Stop/)).not.toBeInTheDocument();

    rerender(
      <GoalBar
        threadId="thread_1"
        goal={paused}
        state={{ live: true, repairing: false }}
        onChanged={() => {}}
      />,
    );
    expect(
      screen.getByText('The round already running will finish; use Stop to end it now.'),
    ).toBeInTheDocument();
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
    expect(screen.getByRole('status')).toHaveTextContent('Reported complete by the agent.');
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    expect(screen.getByRole('menuitem', { name: 'Clear goal' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Edit goal' })).not.toBeInTheDocument();
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
    expect(screen.getByRole('status')).toHaveTextContent('Complete');
    expect(screen.getByRole('status')).toHaveTextContent('2 rounds run');
    expect(screen.getByRole('status')).not.toHaveTextContent('Reported complete by the agent.');
  });
});

// The band's controls are the whole of the goal's management surface, so what
// each one does — and, more importantly, what it does not do — is behaviour
// worth pinning down.
describe('GoalBar controls', () => {
  it('keeps only Pause and the options trigger visible until the menu opens', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('button', { name: 'Pause' })).toBeVisible();
    expect(screen.queryByRole('menuitem')).not.toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: 'Goal options' });
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menuitem', { name: 'Edit goal' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'Clear goal' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Home' });
    expect(screen.getByRole('menuitem', { name: 'Edit goal' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(clearAgentGoal).not.toHaveBeenCalled();
  });

  it('opens at the last menu item on ArrowUp and dismisses on an outside pointer', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.keyDown(screen.getByRole('button', { name: 'Goal options' }), { key: 'ArrowUp' });
    expect(screen.getByRole('menuitem', { name: 'Clear goal' })).toHaveFocus();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(clearAgentGoal).not.toHaveBeenCalled();
  });

  it('returns focus to the menu trigger when an edit is cancelled', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    const trigger = screen.getByRole('button', { name: 'Goal options' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    expect(screen.getByLabelText('Objective')).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(trigger).toHaveFocus();
  });

  it('names a pause that needs input separately from an ordinary user pause', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({ state: 'paused', pauseReason: 'needs_input', pauseDetail: 'Which branch?' })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Needs your input');
    expect(screen.getByRole('status')).toHaveTextContent('Which branch?');
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
  });

  it('names every control and says what it does', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    expect(screen.getByRole('button', { name: 'Pause' })).toHaveAttribute(
      'title',
      expect.stringMatching(/round in flight/),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    const hints: [string, RegExp][] = [
      ['Edit goal', /round in flight is left alone/],
      ['Clear goal', /chat and its history stay/],
    ];
    for (const [name, hint] of hints) {
      const control = screen.getByRole('menuitem', { name });
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

  it('edits a running goal in place, without opening anything', async () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    // In place: the band grows an editor, and no modal appears over the chat.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    // The revision the band was drawn from travels with the write, and the
    // request says nothing about a round bound. That omission is the contract,
    // not an oversight: the server reads an absent `max_rounds` as "leave the
    // bound alone", so editing the words of a goal somebody bounded cannot
    // delete that bound. Sending `null` here would be the removal.
    await waitFor(() =>
      expect(setAgentGoal).toHaveBeenCalledWith('thread_1', {
        goal: { objective: 'ship the release notes' },
        revision: 3,
      }),
    );
    expect(pauseAgentGoal).not.toHaveBeenCalled();
  });

  it('closes the editor on Cancel and abandons what was typed', async () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), { target: { value: 'never mind' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('Objective')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'make `pytest` pass' })).toBeInTheDocument();
    expect(setAgentGoal).not.toHaveBeenCalled();
  });

  it.each([
    ['MacIntel', { metaKey: true }, { ctrlKey: true }],
    ['Win32', { ctrlKey: true }, { metaKey: true }],
  ])(
    'saves on %s with %j+Enter, and leaves plain or %j+Enter alone',
    async (platform, primary, other) => {
      onPlatform(platform);
      render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
      fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
      fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
      const field = screen.getByLabelText('Objective');
      fireEvent.change(field, { target: { value: 'ship the release notes' } });
      // Enter is a newline: an objective is prose and may run to several lines.
      fireEvent.keyDown(field, { key: 'Enter' });
      fireEvent.keyDown(field, { key: 'Enter', ...other });
      expect(setAgentGoal).not.toHaveBeenCalled();
      fireEvent.keyDown(field, { key: 'Enter', ...primary });
      await waitFor(() => expect(setAgentGoal).toHaveBeenCalledTimes(1));
    },
  );

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
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Clear goal' }));
    await waitFor(() => expect(clearAgentGoal).toHaveBeenCalledWith('thread_1'));
    expect(pauseAgentGoal).not.toHaveBeenCalled();
  });

  it('resumes a goal whose stated bound is spent, and says what that does', async () => {
    // The bound is only ever there because the owner wrote it, and nothing in
    // the product edits it — so Resume is the way past it. A disabled button
    // plus "raise the limit" pointed at a form that no longer exists.
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'round_limit',
          pauseDetail: 'the goal reached the round limit its owner set',
          roundsUsed: 10,
          maxRounds: 10,
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    const resume = screen.getByRole('button', { name: 'Resume' });
    expect(resume).toBeEnabled();
    expect(resume.getAttribute('title')).toMatch(/without the round limit/);
    expect(
      screen.getByText('Resuming continues without the 10-round limit you set for this goal.'),
    ).toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(resumeAgentGoal).toHaveBeenCalledWith('thread_1'));
  });

  it('says nothing about lifting a bound the goal has not reached', () => {
    render(
      <GoalBar
        threadId="thread_1"
        goal={goal({
          state: 'paused',
          pauseReason: 'user',
          pauseDetail: 'paused by the user',
          roundsUsed: 3,
          maxRounds: 50,
        })}
        state={idle}
        onChanged={() => {}}
      />,
    );
    expect(screen.queryByText(/without the 50-round limit/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' }).getAttribute('title')).toMatch(
      /queue its next round/,
    );
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
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    // The revision this band holds is exactly what the server refused, so the
    // goal has to be re-read before the owner can try again.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This goal changed while you were editing it.',
    );
  });

  it('keeps the editor open, and the words in it, when an edit is refused', async () => {
    vi.mocked(setAgentGoal).mockRejectedValueOnce(new Error('goal service unavailable'));
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    // Still open, still holding what was typed, and saying why: closing on the
    // way out would throw away the retry.
    expect(await screen.findByRole('alert')).toHaveTextContent('goal service unavailable');
    expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');

    vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 5 });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByLabelText('Objective')).not.toBeInTheDocument());
  });

  it('keeps the draft through the re-read a conflict forces', async () => {
    // A 409 makes the page re-read the goal, and the re-read moves the band's
    // objective under the open editor. Typing the other tab's goal over this
    // owner's words would throw away the retry the open editor exists to allow.
    vi.mocked(setAgentGoal).mockRejectedValueOnce(
      new APIError('goal_revision_conflict', 'This goal changed while you were editing it.'),
    );
    const { rerender } = render(
      <GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), {
      target: { value: 'ship the release notes' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(setAgentGoal).toHaveBeenCalledTimes(1));

    // What the other tab wrote, arriving as the re-read the refusal caused.
    rerender(
      <GoalBar
        threadId="thread_1"
        goal={goal({ objective: 'the other tab’s objective', revision: 4 })}
        state={idle}
        onChanged={() => {}}
      />,
    );

    expect(screen.getByLabelText('Objective')).toHaveValue('ship the release notes');

    // And the retry goes out as written, against the revision the re-read brought.
    vi.mocked(setAgentGoal).mockResolvedValue({ thread_id: 'thread_1', goal: null, revision: 5 });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(setAgentGoal).toHaveBeenLastCalledWith('thread_1', {
        goal: { objective: 'ship the release notes' },
        revision: 4,
      }),
    );
  });

  it('will not save an empty objective', () => {
    render(<GoalBar threadId="thread_1" goal={goal()} state={idle} onChanged={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Goal options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Edit goal' }));
    fireEvent.change(screen.getByLabelText('Objective'), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(setAgentGoal).not.toHaveBeenCalled();
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
