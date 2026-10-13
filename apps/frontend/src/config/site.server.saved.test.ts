import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NEUTRAL_SITE_CONFIG } from './site';

let readRuntimeSiteConfig: (typeof import('./site.server'))['readRuntimeSiteConfig'];

vi.mock('server-only', () => ({}));
vi.mock('next/server', () => ({ connection: vi.fn().mockResolvedValue(undefined) }));
// Next supplies React's RSC cache API to Server Components. This suite runs
// under React 18 without an RSC request; the built-server acceptance check
// verifies deduplication and freshness in the real request context.
vi.mock('react', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react')>()),
  cache: <T>(reader: T): T => reader,
}));

const saved = (appName = 'Saved app') => ({
  branding: { appName, orgName: '', orgUrl: '', statusUrl: '', gatewayUrl: '' },
  features: { rag: false },
});

const response = (value: unknown) => ({ ok: true, json: async () => value });

beforeEach(async () => {
  // Each suite case starts a new web process; reads within one case share its
  // public fallback while the React mock leaves their requests independent.
  vi.resetModules();
  ({ readRuntimeSiteConfig } = await import('./site.server'));
  vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control-plane:8000');
  vi.stubEnv('AGENT_WEB_APP_NAME', 'Old environment branding');
  vi.stubEnv('AGENT_WEB_ORG_URL', 'https://old.example.com');
  vi.stubEnv('AGENT_WEB_FEATURE_RAG', 'true');
  vi.stubEnv('AGENT_DEPLOYMENT_SHA', 'image-version');
  vi.stubEnv('AGENT_WEB_DEPLOYED_AT', '');
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
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

  it('reads fresh saved edits on every request instead of serving the fallback as a cache', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(saved('First name')))
      .mockResolvedValueOnce(response(saved('Edited name')))
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('First name');
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Edited name');
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Edited name');
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it.each(['http', 'network', 'json', 'saved URL'])(
    'retains the last valid appearance after a %s configuration failure',
    async (failure) => {
      const fetch = vi.fn().mockResolvedValueOnce(response(saved()));
      if (failure === 'network') fetch.mockRejectedValueOnce(new Error('unavailable'));
      else if (failure === 'http') fetch.mockResolvedValueOnce({ ok: false });
      else if (failure === 'json') {
        fetch.mockResolvedValueOnce({
          ok: true,
          json: async () => {
            throw new SyntaxError('invalid response');
          },
        });
      } else {
        fetch.mockResolvedValueOnce(
          response({ ...saved(), branding: { ...saved().branding, orgUrl: 'https://%' } }),
        );
      }
      vi.stubGlobal('fetch', fetch);
      const first = await readRuntimeSiteConfig();
      expect(await readRuntimeSiteConfig()).toEqual(first);
    },
  );

  it.each(['headers', 'body'])(
    'bounds a stalled %s read to 400ms and aborts it without needing cancellation to finish',
    async (stall) => {
      vi.useFakeTimers();
      const fetch = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
        expect(init.signal).toBeInstanceOf(AbortSignal);
        return stall === 'headers'
          ? new Promise(() => {})
          : Promise.resolve({ ok: true, json: () => new Promise(() => {}) });
      });
      vi.stubGlobal('fetch', fetch);
      const pending = readRuntimeSiteConfig();
      await vi.advanceTimersByTimeAsync(399);
      const signal = fetch.mock.calls[0][1].signal;
      expect(signal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      expect(await pending).toEqual({
        ...NEUTRAL_SITE_CONFIG,
        deployment: { version: 'image-version', deployedAt: '' },
      });
      expect(signal?.aborted).toBe(true);
    },
  );

  it('keeps valid branding through a timeout and ignores the response that arrives afterward', async () => {
    vi.useFakeTimers();
    let finishBody!: (value: unknown) => void;
    const delayedBody = new Promise((resolve) => {
      finishBody = resolve;
    });
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(saved('Known name')))
      .mockResolvedValueOnce({ ok: true, json: () => delayedBody })
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    await readRuntimeSiteConfig();
    const pending = readRuntimeSiteConfig();
    await vi.advanceTimersByTimeAsync(400);
    expect((await pending).branding.appName).toBe('Known name');
    finishBody(saved('Late name'));
    await vi.advanceTimersByTimeAsync(0);
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Known name');
  });

  it('keeps fallback deployment metadata current and caches only allowlisted public fields', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          ...saved(),
          secret: 'must-not-be-retained',
          agent_access: { allowed: true, permissions: ['agent.admin'] },
          deployment: { version: 'backend-version' },
        }),
      )
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    await readRuntimeSiteConfig();
    vi.stubEnv('AGENT_DEPLOYMENT_SHA', 'new-image-version');
    const fallback = await readRuntimeSiteConfig();
    expect(fallback).toEqual({
      ...saved(),
      deployment: { version: 'new-image-version', deployedAt: '' },
    });
    expect(JSON.stringify(fallback)).not.toContain('must-not-be-retained');
    expect(JSON.stringify(fallback)).not.toContain('agent_access');
  });

  it("never uses one control plane's fallback for another deployment", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(saved('First deployment')))
      .mockRejectedValue(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    await readRuntimeSiteConfig();
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://other-control-plane:8000');
    expect((await readRuntimeSiteConfig()).branding).toEqual(NEUTRAL_SITE_CONFIG.branding);
    vi.stubEnv('AGENT_CONTROL_PLANE_URL', 'http://control-plane:8000/');
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('First deployment');
  });

  it('does not let an older overlapping response replace a newer valid fallback', async () => {
    let finishOld!: (value: unknown) => void;
    const oldBody = new Promise((resolve) => {
      finishOld = resolve;
    });
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: () => oldBody })
      .mockResolvedValueOnce(response(saved('Newer name')))
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);
    const oldRequest = readRuntimeSiteConfig();
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Newer name');
    finishOld(saved('Older name'));
    expect((await oldRequest).branding.appName).toBe('Older name');
    expect((await readRuntimeSiteConfig()).branding.appName).toBe('Newer name');
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
