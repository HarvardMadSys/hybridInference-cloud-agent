import { afterEach, describe, expect, it, vi } from 'vitest';
import { endSession, getSessionUser } from './session';

afterEach(() => vi.unstubAllGlobals());

describe('session failure classification', () => {
  it('treats only a 401 as signed out', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    expect(await getSessionUser()).toBeNull();
  });

  it.each([403, 500, 503])('propagates HTTP %i instead of inventing a sign-out', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Unavailable', { status })));
    await expect(getSessionUser()).rejects.toMatchObject({ statusCode: status });
  });

  it.each([
    'Agent sign-in is not configured correctly. Contact the service administrator.',
    'The deployment credential has expired. Contact the service administrator.',
    'An invalid token is configured. Contact the service administrator.',
  ])('preserves a typed configuration failure regardless of its message: %s', async (message) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              error: { type: 'agent_configuration_error', message },
            },
          }),
          { status: 503 },
        ),
      ),
    );
    await expect(getSessionUser()).rejects.toMatchObject({
      code: 'agent_configuration_error',
      statusCode: 503,
    });
  });

  it.each(['<html>Wrong service</html>', 'null', '{}', '{"id":123}', '{"id":""}'])(
    'rejects an invalid successful response: %s',
    async (body) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
      await expect(getSessionUser()).rejects.toMatchObject({ code: 'invalid_session_response' });
    },
  );
});

describe('ending the session', () => {
  it('resolves once the server has dropped the cookie', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await expect(endSession()).resolves.toBeUndefined();
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/v1\/session\/logout$/), {
      method: 'POST',
      credentials: 'include',
    });
  });

  // The session is a signed token the server cannot revoke, and only a
  // successful answer deletes the cookie that carries it. A proxy's error page
  // means this browser is still signed in, which the caller has to be told.
  it.each([500, 502, 504])('reports HTTP %i, which left the cookie in place', async (status) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('<html>Bad gateway</html>', { status })),
    );
    await expect(endSession()).rejects.toMatchObject({ statusCode: status });
  });

  it('reports a request that never reached the server', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(endSession()).rejects.toMatchObject({ code: 'NETWORK_ERROR' });
  });
});
