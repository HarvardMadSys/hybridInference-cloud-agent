import { afterEach, expect, it, vi } from 'vitest';
import * as client from './client';
import { answerThreadQuestion, listThreadQuestions } from './questions';

afterEach(() => vi.restoreAllMocks());

it('discovers questions from the encoded conversation path with cancellation', async () => {
  const snapshot = { questions: [], job_id: 'job-1', attempt_id: 10 };
  const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(Response.json(snapshot));
  const signal = new AbortController().signal;
  await expect(listThreadQuestions('thread/one', signal)).resolves.toEqual(snapshot);
  expect(fetch).toHaveBeenCalledWith(
    expect.any(String),
    '/v1/agent/threads/thread%2Fone/questions',
    { signal },
  );
});

it('sends answers with their discovered live-turn fence', async () => {
  const fetch = vi
    .spyOn(client, 'fetchWithAuth')
    .mockResolvedValue(Response.json({ accepted: true, reason: null }));
  const answer = {
    request_id: 'request-1',
    job_id: 'job-1',
    attempt_id: 10,
    answers: { choice: ['Option A', 'custom answer'] },
  };
  await answerThreadQuestion('thread-1', answer);
  expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/threads/thread-1/questions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(answer),
  });
});

it('keeps API refusals actionable rather than treating a stale answer as accepted', async () => {
  vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
    Response.json(
      { detail: { code: 'question_expired', message: 'This question expired.' } },
      { status: 409 },
    ),
  );
  await expect(
    answerThreadQuestion('thread-1', {
      request_id: 'request-1',
      job_id: 'job-1',
      attempt_id: 10,
      answers: {},
      reject: true,
    }),
  ).rejects.toMatchObject({
    message: 'This question expired.',
    statusCode: 409,
    code: 'question_expired',
  });
});
