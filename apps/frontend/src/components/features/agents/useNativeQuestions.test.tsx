// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { listThreadQuestions } from '@/lib/api/questions';
import { QUESTION_POLL_MS, useNativeQuestions } from './useNativeQuestions';

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn(),
  answerThreadQuestion: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
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
