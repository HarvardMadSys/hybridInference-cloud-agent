import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NEUTRAL_SITE_CONFIG } from './site';
import { readRuntimeSiteConfig } from './site.server';

vi.mock('server-only', () => ({}));
vi.mock('next/server', () => ({ connection: vi.fn().mockResolvedValue(undefined) }));
// Next supplies React's RSC cache API to Server Components. This suite runs
// under React 18 without an RSC request; the built-server acceptance check
// verifies deduplication and freshness in the real request context.
vi.mock('react', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react')>()),
  cache: <T>(reader: T): T => reader,
}));

beforeEach(() => {
  vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control-plane:8000');
  vi.stubEnv('AGENT_WEB_APP_NAME', 'Old environment branding');
  vi.stubEnv('AGENT_WEB_ORG_URL', 'https://old.example.com');
  vi.stubEnv('AGENT_WEB_FEATURE_RAG', 'true');
  vi.stubEnv('AGENT_DEPLOYMENT_SHA', 'image-version');
  vi.stubEnv('AGENT_WEB_DEPLOYED_AT', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('request-time saved appearance settings', () => {
  it('uses the saved response and fetches without caching', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        branding: { appName: 'Saved app', orgName: '', orgUrl: '', statusUrl: '', gatewayUrl: '' },
        features: { rag: false },
      }),
    });
    vi.stubGlobal('fetch', fetch);
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Saved app');
    expect(fetch).toHaveBeenCalledWith('http://control-plane:8000/v1/agent/site-config', {
      cache: 'no-store',
      signal: expect.any(AbortSignal),
    });
  });

  it.each(['http', 'network', 'json', 'saved URL'])(
    'keeps the root display available after a %s configuration failure',
    async (failure) => {
      const fetch = vi.fn();
      if (failure === 'network') {
        fetch.mockRejectedValue(new Error('unavailable'));
      } else {
        fetch.mockResolvedValue({
          ok: failure !== 'http',
          json: async () => {
            if (failure === 'json') throw new SyntaxError('invalid response');
            return {
              branding: {
                appName: 'Saved app',
                orgName: '',
                orgUrl: 'https://%',
                statusUrl: '',
                gatewayUrl: '',
              },
              features: { rag: true },
            };
          },
        });
      }
      vi.stubGlobal('fetch', fetch);
      expect(await readRuntimeSiteConfig()).toEqual({
        ...NEUTRAL_SITE_CONFIG,
        deployment: { version: 'image-version', deployedAt: '' },
      });
    },
  );
});
