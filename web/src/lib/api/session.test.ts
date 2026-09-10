import { afterEach, describe, expect, it, vi } from 'vitest';
import { getSessionUser } from './session';

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
