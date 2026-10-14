// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CreateAgentJobRequest } from '../agents';

const transport = vi.hoisted(() => ({
  fetchWithAuth: vi.fn<(typeof import('../client'))['fetchWithAuth']>(),
}));

vi.mock('../client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../client')>()),
  fetchWithAuth: transport.fetchWithAuth,
}));

const request: CreateAgentJobRequest = {
  repo: 'owner/repo',
  task_prompt: 'Add regression coverage',
  model: 'glm-5.1',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestId(index: number): string {
  const id = new Headers(transport.fetchWithAuth.mock.calls[index][2]?.headers).get(
    'Idempotency-Key',
  );
  expect(id).toEqual(expect.any(String));
  expect(id).not.toBe('');
  return id!;
}

beforeEach(() => {
  vi.resetModules();
  transport.fetchWithAuth.mockReset();
  sessionStorage.clear();
});

afterEach(() => {
  sessionStorage.clear();
});

describe('job submission recovery', () => {
  it.each(['lost response', 'temporary server failure'] as const)(
    'reuses a pending request after a %s without retrying it automatically',
    async (failure) => {
      const { createAgentJob } = await import('../agents');
      if (failure === 'lost response') {
        transport.fetchWithAuth.mockRejectedValueOnce(new TypeError('Response lost'));
      } else {
        transport.fetchWithAuth.mockResolvedValueOnce(
          jsonResponse({ detail: 'Temporarily unavailable' }, 503),
        );
      }
      transport.fetchWithAuth.mockResolvedValueOnce(
        jsonResponse({ id: 'committed-job', state: 'queued' }, 201),
      );

      await expect(createAgentJob(request)).rejects.toThrow();
      expect(transport.fetchWithAuth).toHaveBeenCalledTimes(1);
      await expect(createAgentJob(request)).resolves.toMatchObject({ id: 'committed-job' });

      expect(transport.fetchWithAuth).toHaveBeenCalledTimes(2);
      expect(requestId(1)).toBe(requestId(0));
      expect(transport.fetchWithAuth.mock.calls[1][2]?.body).toBe(
        transport.fetchWithAuth.mock.calls[0][2]?.body,
      );
    },
  );

  it.each(['error', 'detail.error'] as const)(
    'rotates an edited submission only after an explicit %s idempotency conflict',
    async (envelope) => {
      const { createAgentJob } = await import('../agents');
      const error = { type: 'idempotency_conflict', message: 'Different content under this key' };
      const body = envelope === 'error' ? { error } : { detail: { error } };
      transport.fetchWithAuth
        .mockRejectedValueOnce(new TypeError('Response lost'))
        .mockResolvedValueOnce(jsonResponse(body, 409))
        .mockResolvedValueOnce(jsonResponse({ id: 'edited-job', state: 'queued' }, 201));

      await expect(createAgentJob(request)).rejects.toThrow('Response lost');
      const edited = { ...request, task_prompt: 'Add coverage and documentation' };
      await expect(createAgentJob(edited)).resolves.toMatchObject({ id: 'edited-job' });

      expect(transport.fetchWithAuth).toHaveBeenCalledTimes(3);
      expect(requestId(1)).toBe(requestId(0));
      expect(requestId(2)).not.toBe(requestId(1));
      expect(
        transport.fetchWithAuth.mock.calls.map(([, , init]) => JSON.parse(init!.body as string)),
      ).toEqual([request, edited, edited]);
    },
  );

  it('keeps the same request after a conflict that does not reject its idempotency key', async () => {
    const { createAgentJob } = await import('../agents');
    transport.fetchWithAuth
      .mockResolvedValueOnce(
        jsonResponse(
          { detail: { error: { type: 'job_admission_conflict', message: 'Try again later' } } },
          409,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'accepted-job', state: 'queued' }, 201));

    await expect(createAgentJob(request)).rejects.toThrow('Try again later');
    expect(transport.fetchWithAuth).toHaveBeenCalledTimes(1);
    await expect(createAgentJob(request)).resolves.toMatchObject({ id: 'accepted-job' });

    expect(transport.fetchWithAuth).toHaveBeenCalledTimes(2);
    expect(requestId(1)).toBe(requestId(0));
  });

  it('recovers a pending request across a page reload and retires it after success', async () => {
    const beforeReload = await import('../agents');
    transport.fetchWithAuth
      .mockRejectedValueOnce(new TypeError('Response lost'))
      .mockResolvedValueOnce(jsonResponse({ id: 'recovered-job', state: 'queued' }, 201))
      .mockResolvedValueOnce(jsonResponse({ id: 'next-job', state: 'queued' }, 201));

    await expect(beforeReload.createAgentJob(request)).rejects.toThrow('Response lost');
    const pendingId = requestId(0);

    // A page reload discards module memory while retaining this tab's storage.
    vi.resetModules();
    const afterReload = await import('../agents');
    expect(afterReload.createAgentJob).not.toBe(beforeReload.createAgentJob);
    await expect(afterReload.createAgentJob(request)).resolves.toMatchObject({
      id: 'recovered-job',
    });
    expect(requestId(1)).toBe(pendingId);

    vi.resetModules();
    const afterSuccess = await import('../agents');
    await expect(afterSuccess.createAgentJob(request)).resolves.toMatchObject({ id: 'next-job' });
    expect(requestId(2)).not.toBe(pendingId);
    expect(transport.fetchWithAuth).toHaveBeenCalledTimes(3);
  });
});
