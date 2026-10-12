// Tests for the conversation message client: one send route, and the queue.
//
// The properties that matter are the ones a lost response or a second tab
// would break: a resend keeps its key, different content gets a new one, and a
// refused edit hands back the message as it is now.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  conflictCurrent,
  deletePendingMessage,
  editPendingMessage,
  getThreadQueue,
  resumeThreadQueue,
  submitThreadMessage,
} from '../pendingMessages';
import * as client from '../client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const waiting = {
  id: 'apmsg_1',
  thread_id: 'athr_1',
  position: 7,
  state: 'queued',
  revision: 1,
  prompt: 'and then',
};

describe('pending messages api', () => {
  let fetchWithAuth: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchWithAuth = vi.spyOn(client, 'fetchWithAuth');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sends a message to the conversation, not to one of its turns', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ pending: waiting, job: null }, 201));

    const result = await submitThreadMessage('athr_1', { prompt: 'please add a test' });

    expect(result.pending?.id).toBe('apmsg_1');
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/threads/athr_1/messages',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ prompt: 'please add a test' }),
      }),
    );
  });

  it('trims surrounding whitespace from a prompt before sending it', async () => {
    fetchWithAuth.mockResolvedValue(jsonResponse({ pending: waiting, job: null }, 201));

    await submitThreadMessage('athr_1', { prompt: '\n  please add a test\nand docs  \n' });

    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/threads/athr_1/messages',
      expect.objectContaining({
        body: JSON.stringify({ prompt: 'please add a test\nand docs' }),
      }),
    );
  });

  it('keeps the idempotency key while the response is lost', async () => {
    fetchWithAuth
      .mockRejectedValueOnce(new TypeError('network error'))
      .mockResolvedValueOnce(
        jsonResponse({ pending: null, job: { id: 'ajob_child', state: 'queued' } }, 201),
      );

    await expect(submitThreadMessage('athr_1', { prompt: 'and then' })).rejects.toThrow();
    await expect(submitThreadMessage('athr_1', { prompt: 'and then' })).resolves.toMatchObject({
      job: { id: 'ajob_child' },
    });

    const first = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const second = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(second['Idempotency-Key']).toBe(first['Idempotency-Key']);
  });

  it('drops the key once an answer arrives, so the next message is a new one', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201))
      .mockResolvedValueOnce(jsonResponse({ pending: { ...waiting, id: 'apmsg_2' } }, 201));

    await submitThreadMessage('athr_1', { prompt: 'one' });
    await submitThreadMessage('athr_1', { prompt: 'two' });

    const first = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const second = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(second['Idempotency-Key']).not.toBe(first['Idempotency-Key']);
  });

  it('rotates the key when the server reports different content under it', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: { error: { type: 'idempotency_conflict', message: 'different content' } } },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201));

    await expect(
      submitThreadMessage('athr_edited', { prompt: 'edited text' }),
    ).resolves.toMatchObject({ pending: { id: 'apmsg_1' } });

    expect(fetchWithAuth).toHaveBeenCalledTimes(2);
    const first = fetchWithAuth.mock.calls[0][2]?.headers as Record<string, string>;
    const second = fetchWithAuth.mock.calls[1][2]?.headers as Record<string, string>;
    expect(second['Idempotency-Key']).not.toBe(first['Idempotency-Key']);
  });

  it('keeps a caller-owned key across lost and successful responses', async () => {
    fetchWithAuth
      .mockRejectedValueOnce(new TypeError('network error'))
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201))
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201));
    const options = { requestId: 'review-operation' };

    await expect(
      submitThreadMessage('athr_review', { prompt: 'review changes' }, options),
    ).rejects.toThrow('network error');
    await submitThreadMessage('athr_review', { prompt: 'review changes' }, options);
    await submitThreadMessage('athr_review', { prompt: 'review changes' }, options);

    expect(fetchWithAuth).toHaveBeenCalledTimes(3);
    for (const call of fetchWithAuth.mock.calls) {
      expect(call[2]?.headers).toMatchObject({ 'Idempotency-Key': 'review-operation' });
    }
  });

  it('sends the idle and latest-turn preconditions and surfaces a stale refusal without retrying', async () => {
    fetchWithAuth.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: { type: 'conversation_changed', message: 'The conversation changed' },
          },
        },
        409,
      ),
    );
    const body = {
      prompt: 'Watch the PR and merge',
      require_idle: true,
      expected_latest_job_id: 'ajob_latest',
    };
    const options = { requestId: 'watch-pr:v2:athr_guarded:ajob_latest' };

    await expect(submitThreadMessage('athr_guarded', body, options)).rejects.toMatchObject({
      code: 'conversation_changed',
      statusCode: 409,
    });

    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/threads/athr_guarded/messages',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'Idempotency-Key': options.requestId }),
        body: JSON.stringify(body),
      }),
    );
  });

  it('does not rotate a caller-owned key after an idempotency conflict', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: { error: { type: 'idempotency_conflict', message: 'different content' } } },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201));
    const options = { requestId: 'review-conflict-operation' };

    await expect(
      submitThreadMessage('athr_review_conflict', { prompt: 'review changes' }, options),
    ).rejects.toMatchObject({ code: 'idempotency_conflict' });
    expect(fetchWithAuth).toHaveBeenCalledTimes(1);
    await submitThreadMessage('athr_review_conflict', { prompt: 'review changes' }, options);

    for (const call of fetchWithAuth.mock.calls) {
      expect(call[2]?.headers).toMatchObject({ 'Idempotency-Key': 'review-conflict-operation' });
    }
  });

  it('leaves an ordinary pending message key untouched by caller-owned sends', async () => {
    fetchWithAuth
      .mockRejectedValueOnce(new TypeError('network error'))
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: { error: { type: 'idempotency_conflict', message: 'different content' } } },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201))
      .mockResolvedValueOnce(jsonResponse({ pending: waiting, job: null }, 201));

    await expect(
      submitThreadMessage('athr_separate_keys', { prompt: 'ordinary message' }),
    ).rejects.toThrow('network error');
    await expect(
      submitThreadMessage(
        'athr_separate_keys',
        { prompt: 'review changes' },
        { requestId: 'separate-review-operation' },
      ),
    ).rejects.toMatchObject({ code: 'idempotency_conflict' });
    await submitThreadMessage(
      'athr_separate_keys',
      { prompt: 'review changes' },
      { requestId: 'separate-review-operation' },
    );
    await submitThreadMessage('athr_separate_keys', { prompt: 'ordinary message' });

    const keys = fetchWithAuth.mock.calls.map(
      (call: Parameters<typeof client.fetchWithAuth>) =>
        (call[2]?.headers as Record<string, string>)['Idempotency-Key'],
    );
    expect(keys[0]).not.toBe('separate-review-operation');
    expect(keys).toEqual([
      keys[0],
      'separate-review-operation',
      'separate-review-operation',
      keys[0],
    ]);
  });

  it('edits at the revision read, and a refusal carries the message as it is now', async () => {
    const current = { ...waiting, revision: 2, prompt: 'from the other tab' };
    fetchWithAuth.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: { type: 'revision_conflict', message: 'changed', current },
          },
        },
        409,
      ),
    );

    const refused = await editPendingMessage('athr_1', 'apmsg_1', 1, {
      prompt: 'mine',
      state: 'queued',
    }).catch((cause: unknown) => cause);

    expect(conflictCurrent(refused)).toEqual(current);
    expect(fetchWithAuth).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/threads/athr_1/messages/apmsg_1',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ revision: 1, prompt: 'mine', state: 'queued' }),
      }),
    );
  });

  it('deletes with the revision in the query and continues a paused queue', async () => {
    fetchWithAuth
      .mockResolvedValueOnce(jsonResponse({ pending: null, job: null }))
      .mockResolvedValueOnce(
        jsonResponse({ pending: null, job: { id: 'ajob_next' }, queue_paused_reason: null }),
      );

    await deletePendingMessage('athr_1', 'apmsg_1', 3);
    const resumed = await resumeThreadQueue('athr_1');

    expect(fetchWithAuth.mock.calls[0][1]).toBe(
      '/v1/agent/threads/athr_1/messages/apmsg_1?revision=3',
    );
    expect(fetchWithAuth.mock.calls[0][2]).toMatchObject({ method: 'DELETE' });
    expect(fetchWithAuth.mock.calls[1][1]).toBe('/v1/agent/threads/athr_1/queue/resume');
    expect(resumed.job?.id).toBe('ajob_next');
  });

  it('reads the queue without the transcript', async () => {
    fetchWithAuth.mockResolvedValue(
      jsonResponse({
        thread_id: 'athr_1',
        pending_messages: [waiting],
        queue_paused_reason: 'stopped',
        latest_job_id: 'ajob_1',
        latest_turn_no: 1,
        latest_job_state: 'cancelled',
      }),
    );

    const queue = await getThreadQueue('athr_1');

    expect(queue.queue_paused_reason).toBe('stopped');
    expect(fetchWithAuth.mock.calls[0][1]).toBe('/v1/agent/threads/athr_1/messages');
  });
});
