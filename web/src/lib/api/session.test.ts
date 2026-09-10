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
});
