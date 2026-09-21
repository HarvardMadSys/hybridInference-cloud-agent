// @vitest-environment jsdom

// `client.ts` is the one file the move rewrote rather than copied, so it arrives
// with no inherited coverage. These cases pin the two things the rewrite changed:
// the credential is the cookie and nothing else, and a 401 is terminal.

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { APIError } from '@/lib/utils/errors';
import { AUTH_EXPIRED_EVENT, fetchWithAuth, jsonOrThrow, safeFetch } from './client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('fetchWithAuth', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
  });

  it('sends the session cookie and no bearer token', async () => {
    await fetchWithAuth('https://api.example.org', '/v1/agent/jobs');

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.example.org/v1/agent/jobs');
    expect(init.credentials).toBe('include');
    expect(new Headers(init.headers ?? {}).get('Authorization')).toBeNull();
  });

  it('keeps credentials on a caller that supplied its own headers', async () => {
    await fetchWithAuth('', '/v1/agent/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    const init = fetchMock.mock.calls[0][1];
    expect(init.credentials).toBe('include');
    expect(init.method).toBe('POST');
  });

  it('announces an expired session once and does not retry it', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 401 }));
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);

    const resp = await fetchWithAuth('', '/v1/agent/jobs');

    expect(resp.status).toBe(401);
    expect(expired).toHaveBeenCalledOnce();
    // The source client refreshed and replayed here. There is no refresh token
    // in this service, so a second attempt would only delay the redirect.
    expect(fetchMock).toHaveBeenCalledOnce();
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  });

  it('says nothing about the session on an ordinary failure', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 500 }));
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);

    await fetchWithAuth('', '/v1/agent/jobs');

    expect(expired).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  });
});

describe('safeFetch', () => {
  it('turns a connectivity failure into NETWORK_ERROR', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    await expect(safeFetch('/x')).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
  });

  it('lets a deliberate abort through unchanged', async () => {
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort));

    await expect(safeFetch('/x')).rejects.toBe(abort);
  });
});

describe('jsonOrThrow', () => {
  it('returns the body of a successful response', async () => {
    await expect(jsonOrThrow(jsonResponse(200, { id: 'ajob_1' }))).resolves.toEqual({
      id: 'ajob_1',
    });
  });

  it('classifies a non-JSON error page by status', async () => {
    const resp = new Response('<html>bad gateway</html>', { status: 502 });

    await expect(jsonOrThrow(resp)).rejects.toMatchObject({
      code: 'SERVICE_UNAVAILABLE',
      statusCode: 502,
    });
  });

  it('carries a FastAPI detail message through verbatim', async () => {
    const resp = jsonResponse(400, { detail: 'repo is not connected' });

    await expect(jsonOrThrow(resp)).rejects.toMatchObject({
      message: 'repo is not connected',
    });
  });

  it('unwraps a structured error nested under FastAPI detail', async () => {
    const resp = jsonResponse(409, {
      detail: {
        error: {
          type: 'native_fork_failed',
          message: 'historical fork could not be reconciled',
        },
      },
    });

    await expect(jsonOrThrow(resp)).rejects.toMatchObject({
      code: 'native_fork_failed',
      message: 'historical fork could not be reconciled',
      statusCode: 409,
    });
  });

  it('throws an APIError, so callers can narrow on it', async () => {
    await expect(
      jsonOrThrow(jsonResponse(500, { error: { message: 'boom' } })),
    ).rejects.toBeInstanceOf(APIError);
  });
});
