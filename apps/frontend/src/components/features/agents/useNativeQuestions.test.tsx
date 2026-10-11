// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import {
  answerThreadQuestion,
  listThreadQuestions,
  type AgentQuestionsApi,
} from '@/lib/api/questions';
import { AgentReadProvider, useQuestionReads } from './AgentReadProvider';
import { QUESTION_POLL_MS, useNativeQuestions } from './useNativeQuestions';

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn(),
  answerThreadQuestion: vi.fn(),
}));
beforeEach(() => vi.resetAllMocks());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const activeTurn = { threadId: 'thread', jobId: 'job', attemptId: 1, active: true };
const pending: AgentQuestionsApi = {
  job_id: 'job',
  attempt_id: 1,
  questions: [{ request_id: 'question', questions: [] }],
};

it('shares row and detail polls and bypasses the cached question after an accepted answer', async () => {
  vi.mocked(listThreadQuestions).mockResolvedValue(pending);
  vi.mocked(answerThreadQuestion).mockResolvedValue({ accepted: true, reason: null });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <AgentReadProvider userId="alice">{children}</AgentReadProvider>
  );
  const { result } = renderHook(
    () => ({
      row: useNativeQuestions(activeTurn),
      detail: useNativeQuestions(activeTurn),
    }),
    { wrapper },
  );
  await act(async () => {});
  expect(listThreadQuestions).toHaveBeenCalledOnce();
  expect(result.current.row.request).toEqual(result.current.detail.request);
  vi.mocked(listThreadQuestions).mockResolvedValue({ ...pending, questions: [] });
  await act(async () => {
    await result.current.detail.answer(result.current.detail.request!, {});
    await result.current.row.refresh();
  });
  expect(listThreadQuestions).toHaveBeenCalledTimes(2);
  expect(result.current.detail.request).toBeNull();
  expect(result.current.row.request).toBeNull();
});

it('keeps a known question without showing an error when a shared poll is invalidated', async () => {
  vi.useFakeTimers();
  let finish!: (value: AgentQuestionsApi) => void;
  vi.mocked(listThreadQuestions)
    .mockResolvedValueOnce(pending)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue({ ...pending, questions: [] });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <AgentReadProvider userId="alice">{children}</AgentReadProvider>
  );
  const { result } = renderHook(
    () => ({ native: useNativeQuestions(activeTurn), reads: useQuestionReads() }),
    { wrapper },
  );
  await act(async () => {});
  expect(result.current.native.request?.request_id).toBe('question');
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  await act(async () => result.current.reads.invalidate());
  expect(result.current.native.error).toBeNull();
  expect(result.current.native.request?.request_id).toBe('question');
  await act(async () => finish({ ...pending, questions: [] }));
  expect(result.current.native.request?.request_id).toBe('question');
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS));
  expect(result.current.native.error).toBeNull();
  expect(result.current.native.request).toBeNull();
});

it('hides the previous owner question immediately while the new owner read is pending', async () => {
  let finish!: (value: AgentQuestionsApi) => void;
  vi.mocked(listThreadQuestions)
    .mockResolvedValueOnce(pending)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
  let userId = 'alice';
  const wrapper = ({ children }: { children: ReactNode }) => (
    <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
  );
  const { result, rerender } = renderHook(() => useNativeQuestions(activeTurn), { wrapper });
  await act(async () => {});
  expect(result.current.request?.request_id).toBe('question');
  userId = 'bob';
  rerender();
  expect(result.current.request).toBeNull();
  await act(async () =>
    finish({ ...pending, questions: [{ request_id: 'bob-question', questions: [] }] }),
  );
  expect(result.current.request?.request_id).toBe('bob-question');
  expect(listThreadQuestions).toHaveBeenCalledTimes(2);
});

it('preserves discovered questions while hidden and refreshes on return', async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.mocked(listThreadQuestions).mockResolvedValue({
    job_id: 'job',
    attempt_id: 1,
    questions: [{ request_id: 'question', questions: [] }],
  });
  const { result } = renderHook(() =>
    useNativeQuestions({
      threadId: 'thread',
      jobId: 'job',
      attemptId: 1,
      active: true,
    }),
  );
  await act(async () => {});
  expect(result.current.request?.request_id).toBe('question');
  visibility.mockReturnValue('hidden');
  await act(async () => vi.advanceTimersByTimeAsync(QUESTION_POLL_MS * 5));
  expect(listThreadQuestions).toHaveBeenCalledOnce();
  expect(result.current.request?.request_id).toBe('question');
  visibility.mockReturnValue('visible');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(listThreadQuestions).toHaveBeenCalledTimes(2);
});
