// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  answerThreadQuestion,
  listThreadQuestions,
  type AgentQuestionRequestApi,
  type AgentQuestionsApi,
} from '@/lib/api/questions';
import { NativeQuestionPrompt } from './NativeQuestionPrompt';
import { QUESTION_POLL_MS } from './useNativeQuestions';

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn(),
  answerThreadQuestion: vi.fn(),
}));
const list = vi.mocked(listThreadQuestions);
const answer = vi.mocked(answerThreadQuestion);
const props = { threadId: 'thread-1', jobId: 'job-1', attemptId: 10, active: true };
const request: AgentQuestionRequestApi = {
  request_id: 'request-1',
  questions: [
    {
      id: 'direction',
      header: 'Direction',
      question: 'Which direction should we take?',
      multiple: false,
      options: [
        { label: 'North', description: 'Go north.' },
        { label: 'South', description: 'Go south.' },
      ],
      allow_custom: true,
    },
  ],
};
function snapshot(questions = [request]): AgentQuestionsApi {
  return { job_id: props.jobId, attempt_id: props.attemptId, questions };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.useFakeTimers();
  list.mockReset().mockResolvedValue(snapshot());
  answer.mockReset().mockResolvedValue({ accepted: true, reason: null });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('opens a discovered question, sends its selected answer, and checks for another request', async () => {
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  expect(screen.getByRole('dialog', { name: 'The agent has a question' })).toBeInTheDocument();
  expect(screen.getByRole('radio', { name: 'North' })).toHaveFocus();
  expect(screen.getByRole('radio', { name: 'North' })).toHaveAccessibleDescription('Go north.');
  expect(screen.getByRole('button', { name: 'Send answer' })).toBeDisabled();
  fireEvent.click(screen.getByRole('radio', { name: 'North' }));
  list.mockResolvedValue(snapshot([]));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  expect(answer).toHaveBeenCalledWith('thread-1', {
    request_id: 'request-1',
    job_id: 'job-1',
    attempt_id: 10,
    answers: { direction: ['North'] },
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(list).toHaveBeenCalledTimes(2);
});

it('supports multiple questions, multiple options, and a custom answer', async () => {
  list.mockResolvedValue(
    snapshot([
      {
        ...request,
        questions: [
          { ...request.questions[0], multiple: true },
          {
            id: 'details',
            header: 'Details',
            question: 'What else should we know?',
            multiple: false,
            options: [],
            allow_custom: true,
          },
        ],
      },
    ]),
  );
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  const group = screen.getByRole('group', { name: /Which direction/ });
  fireEvent.click(within(group).getByRole('checkbox', { name: 'North' }));
  fireEvent.click(within(group).getByRole('checkbox', { name: 'South' }));
  fireEvent.click(within(group).getByRole('checkbox', { name: 'Other' }));
  fireEvent.change(within(group).getByRole('textbox', { name: 'Your answer' }), {
    target: { value: ' East ' },
  });
  expect(screen.getByRole('button', { name: 'Send answer' })).toBeDisabled();
  fireEvent.change(
    within(screen.getByRole('group', { name: /What else/ })).getByRole('textbox', {
      name: 'Your answer',
    }),
    { target: { value: 'Avoid traffic.' } },
  );
  list.mockResolvedValue(snapshot([]));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  expect(answer).toHaveBeenCalledWith(
    'thread-1',
    expect.objectContaining({
      answers: { direction: ['North', 'South', 'East'], details: ['Avoid traffic.'] },
    }),
  );
});

it('lets a single choice use a custom answer instead of an option', async () => {
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  fireEvent.click(screen.getByRole('radio', { name: 'North' }));
  fireEvent.click(screen.getByRole('radio', { name: 'Other' }));
  expect(screen.getByRole('radio', { name: 'North' })).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Send answer' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'East' } });
  list.mockResolvedValue(snapshot([]));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  expect(answer).toHaveBeenCalledWith(
    'thread-1',
    expect.objectContaining({ answers: { direction: ['East'] } }),
  );
});

it('can skip a question and disables controls while its decision is being sent', async () => {
  const pending = deferred<Awaited<ReturnType<typeof answer>>>();
  answer.mockReturnValue(pending.promise);
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Skip question' }));
  expect(answer).toHaveBeenCalledWith('thread-1', {
    request_id: 'request-1',
    job_id: 'job-1',
    attempt_id: 10,
    answers: {},
    reject: true,
  });
  expect(screen.getByRole('button', { name: 'Sending…' })).toBeDisabled();
  expect(screen.getByRole('radio', { name: 'North' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Answer later' })).toBeDisabled();
  list.mockResolvedValue(snapshot([]));
  await act(async () => pending.resolve({ accepted: true, reason: null }));
});

it('preserves an answer through a failed submission and polling so it can be retried', async () => {
  answer.mockRejectedValueOnce(new Error('Could not reach the agent.'));
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  fireEvent.click(screen.getByRole('radio', { name: 'South' }));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  expect(screen.getByRole('alert')).toHaveTextContent('Could not reach the agent.');
  expect(screen.getByRole('radio', { name: 'South' })).toBeChecked();
  list.mockResolvedValue(snapshot([]));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  expect(answer).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('discovers a question on the next poll and can retry an unavailable discovery', async () => {
  list.mockRejectedValueOnce(new Error('Offline')).mockResolvedValue(snapshot([]));
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  expect(screen.getByRole('status')).toHaveTextContent('Could not check for questions.');
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  });
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  list.mockResolvedValue(snapshot());
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

it('dismisses without rejecting, restores focus, and lets the owner reopen the question', async () => {
  const composer = document.createElement('textarea');
  document.body.appendChild(composer);
  composer.focus();
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  fireEvent.click(screen.getByRole('radio', { name: 'South' }));
  fireEvent.keyDown(screen.getByRole('radio', { name: 'North' }), { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(composer).toHaveFocus();
  expect(answer).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Review question' }));
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.getByRole('radio', { name: 'South' })).toBeChecked();
  composer.remove();
});

it('traps keyboard focus within the dialog', async () => {
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  const first = screen.getByRole('radio', { name: 'North' });
  const last = screen.getByRole('button', { name: 'Answer later' });
  fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
  expect(last).toHaveFocus();
  fireEvent.keyDown(last, { key: 'Tab' });
  expect(first).toHaveFocus();
});

it.each([
  { ...props, threadId: 'thread-2', jobId: 'job-2' },
  { ...props, jobId: 'next-job' },
  { ...props, attemptId: 11 },
  { ...props, active: false },
])('ignores discovery from a previous thread or active generation: %j', async (nextProps) => {
  const pending = deferred<AgentQuestionsApi>();
  list.mockReturnValueOnce(pending.promise).mockResolvedValue(snapshot([]));
  const { rerender } = render(<NativeQuestionPrompt {...props} />);
  await act(async () => {
    rerender(<NativeQuestionPrompt {...nextProps} />);
  });
  await act(async () => pending.resolve(snapshot()));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('rejects host snapshots belonging to another live attempt', async () => {
  list.mockResolvedValue({ ...snapshot(), attempt_id: 11 });
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('removes pending questions and stops discovery as soon as the turn settles', async () => {
  let rerender!: ReturnType<typeof render>['rerender'];
  await act(async () => {
    ({ rerender } = render(<NativeQuestionPrompt {...props} />));
  });
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  await act(async () => {
    rerender(<NativeQuestionPrompt {...props} active={false} />);
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  list.mockClear();
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS * 3));
  expect(list).not.toHaveBeenCalled();
});

it('does not revive a question from a poll begun before its accepted answer', async () => {
  await act(async () => {
    render(<NativeQuestionPrompt {...props} />);
  });
  const pending = deferred<AgentQuestionsApi>();
  list.mockReturnValueOnce(pending.promise).mockResolvedValue(snapshot([]));
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  fireEvent.click(screen.getByRole('radio', { name: 'North' }));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
  });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await act(async () => pending.resolve(snapshot()));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(list).toHaveBeenCalledTimes(3);
});
