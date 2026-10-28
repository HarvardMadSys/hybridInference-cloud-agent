import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('server-only', () => ({}));
vi.mock('next/server', () => ({ connection: vi.fn(async () => {}) }));
import { readRuntimeAuthConfig } from './auth.server';
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe('server-rendered login mode', () => {
  it('keeps the Gateway default for legacy standalone web deployments', async () => {
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', '');
    await expect(readRuntimeAuthConfig()).resolves.toEqual({
      mode: 'gateway',
      signup_enabled: false,
    });
  });
  it('reads local mode on every request and copies only its public fields', async () => {
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control.test/');
    const fetch = vi
      .fn()
      .mockImplementation(
        async () =>
          new Response(
            JSON.stringify({ mode: 'local', signup_enabled: true, secret: 'never-copy' }),
          ),
      );
    vi.stubGlobal('fetch', fetch);
    await expect(readRuntimeAuthConfig()).resolves.toEqual({ mode: 'local', signup_enabled: true });
    await readRuntimeAuthConfig();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][0]).toBe('http://control.test/v1/auth/config');
  });
  it('an unavailable configured authority shows loading/retry rather than a Gateway login', async () => {
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control.test');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 503 })));
    await expect(readRuntimeAuthConfig()).resolves.toBeNull();
  });
  it('bounds a stalled response body as well as response headers', async () => {
    vi.useFakeTimers();
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control.test');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => new Promise(() => {}) }),
    );
    const read = readRuntimeAuthConfig();
    await vi.advanceTimersByTimeAsync(401);
    await expect(read).resolves.toBeNull();
  });
});
