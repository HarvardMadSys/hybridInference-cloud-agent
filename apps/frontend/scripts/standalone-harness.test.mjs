import { EventEmitter } from 'node:events';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  buildEnvironmentForVariant,
  sanitizeRuntimeSiteEnvironment,
  startStandalone,
  waitForResponse,
} from './standalone-harness.mjs';

function fakeChild({ spawned = true } = {}) {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.pid = spawned ? 12_345 : undefined;
  child.kill = vi.fn(() => true);
  return child;
}

function fakeSignalTarget() {
  const target = new EventEmitter();
  target.pid = 54_321;
  target.kill = vi.fn();
  return target;
}

async function startWith(child, options = {}) {
  return startStandalone({
    spawnProcess: () => child,
    reserveServerPort: async () => 4_321,
    signalTarget: fakeSignalTarget(),
    ...options,
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('standalone environment policy', () => {
  it('forces public build paths to match the selected variant', () => {
    const variant = { basePath: '/agents', apiUrl: '/agents/api' };

    expect(
      buildEnvironmentForVariant(variant, {
        NEXT_PUBLIC_BASE_PATH: '/wrong',
        NEXT_PUBLIC_AGENT_API_URL: 'https://wrong.invalid',
        UNRELATED: 'kept',
      }),
    ).toEqual({
      NEXT_PUBLIC_BASE_PATH: '/agents',
      NEXT_PUBLIC_AGENT_API_URL: '/agents/api',
      UNRELATED: 'kept',
    });
  });

  it('removes every ambient runtime-site key and retains unrelated values', () => {
    expect(
      sanitizeRuntimeSiteEnvironment({
        AGENT_WEB_APP_NAME: 'ambient',
        AGENT_WEB_FUTURE_VALUE: 'ambient',
        AGENT_DEPLOYMENT_SHA: 'ambient',
        AGENT_CONTROL_PLANE_URL: 'https://real-control-plane.invalid',
        UNRELATED: 'kept',
      }),
    ).toEqual({ UNRELATED: 'kept' });
  });
});

describe('waitForResponse', () => {
  it('aborts a hanging request at the total deadline and keeps diagnostics', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(
      (_url, { signal }) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(signal.reason), { once: true });
        }),
    );
    const server = {
      child: { exitCode: null, signalCode: null },
      logs: ['server accepted the connection\n'],
      terminated: false,
    };

    const waiting = waitForResponse('http://127.0.0.1:4321/ready', {
      server,
      timeoutMs: 250,
      pollIntervalMs: 100,
    });
    const rejected = expect(waiting).rejects.toThrow(
      'did not answer within 250ms: request did not complete before the deadline\n' +
        'server accepted the connection',
    );
    await vi.advanceTimersByTimeAsync(250);

    await rejected;
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  });

  it('keeps the response status when an unacceptable response body hangs', async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockImplementation((_url, { signal }) =>
      Promise.resolve({
        status: 503,
        arrayBuffer: () =>
          new Promise((_resolve, reject) => {
            signal.addEventListener('abort', () => reject(signal.reason), { once: true });
          }),
      }),
    );

    const waiting = waitForResponse('http://127.0.0.1:4321/ready', {
      timeoutMs: 250,
      acceptResponse: () => false,
    });
    const rejected = expect(waiting).rejects.toThrow(
      'answered 503; response did not complete before the deadline',
    );
    await vi.advanceTimersByTimeAsync(250);

    await rejected;
  });

  it('returns an accepted buffered response with its status and headers', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('ready', {
        status: 202,
        statusText: 'Accepted',
        headers: { 'x-runtime-fixture': 'complete' },
      }),
    );

    const response = await waitForResponse('http://127.0.0.1:4321/ready');
    const replay = response.clone();

    expect(response.status).toBe(202);
    expect(response.statusText).toBe('Accepted');
    expect(response.headers.get('x-runtime-fixture')).toBe('complete');
    await expect(response.text()).resolves.toBe('ready');
    await expect(replay.text()).resolves.toBe('ready');
  });

  it('keeps the total deadline through an accepted partial response body', async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockImplementation((_url, { signal }) =>
      Promise.resolve(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode('partial'));
              signal.addEventListener('abort', () => controller.error(signal.reason), {
                once: true,
              });
            },
          }),
          {
            status: 200,
            headers: { 'x-runtime-fixture': 'partial' },
          },
        ),
      ),
    );

    const waiting = waitForResponse('http://127.0.0.1:4321/ready', { timeoutMs: 50 });
    const rejected = expect(waiting).rejects.toThrow(
      'did not answer within 50ms: answered 200; response did not complete before the deadline',
    );
    await vi.advanceTimersByTimeAsync(50);

    await rejected;
  });
});

describe('standalone process lifecycle', () => {
  it('settles stop when spawning the child fails', async () => {
    const child = fakeChild({ spawned: false });
    const server = await startWith(child);
    const stopping = server.stop();

    child.emit('error', new Error('spawn ENOENT'));

    await expect(stopping).resolves.toBeUndefined();
    expect(server.logs).toContain('spawn error: spawn ENOENT');
  });

  it('shares one stop operation among concurrent callers', async () => {
    const child = fakeChild();
    const server = await startWith(child);

    const first = server.stop();
    const second = server.stop();
    expect(second).toBe(first);
    expect(child.kill).toHaveBeenCalledTimes(1);
    expect(child.kill).toHaveBeenCalledWith('SIGTERM');

    child.exitCode = 0;
    child.emit('exit', 0, null);
    await expect(first).resolves.toBeUndefined();
  });

  it('bounds the forced-kill wait when a child never exits', async () => {
    vi.useFakeTimers();
    const child = fakeChild();
    const server = await startWith(child, { stopGraceMs: 100, forceKillWaitMs: 50 });
    const stopping = server.stop();
    const rejected = expect(stopping).rejects.toThrow(
      'Production server did not exit after SIGKILL',
    );

    await vi.advanceTimersByTimeAsync(100);
    expect(child.kill).toHaveBeenNthCalledWith(2, 'SIGKILL');
    await vi.advanceTimersByTimeAsync(50);

    await rejected;
  });

  it('coalesces repeated process signals until the child stops', async () => {
    const child = fakeChild();
    const signalTarget = fakeSignalTarget();
    const server = await startWith(child, { signalTarget });

    expect(signalTarget.listenerCount('SIGINT')).toBe(1);
    expect(signalTarget.listenerCount('SIGTERM')).toBe(1);
    signalTarget.emit('SIGTERM');
    signalTarget.emit('SIGTERM');
    signalTarget.emit('SIGINT');
    expect(child.kill).toHaveBeenCalledWith('SIGTERM');
    expect(child.kill).toHaveBeenCalledTimes(1);
    expect(signalTarget.kill).not.toHaveBeenCalled();
    expect(signalTarget.listenerCount('SIGINT')).toBe(1);
    expect(signalTarget.listenerCount('SIGTERM')).toBe(1);

    child.signalCode = 'SIGTERM';
    child.emit('exit', null, 'SIGTERM');
    await server.stop();
    await Promise.resolve();

    expect(signalTarget.kill).toHaveBeenCalledWith(signalTarget.pid, 'SIGTERM');
    expect(signalTarget.kill).toHaveBeenCalledTimes(1);
    expect(signalTarget.listenerCount('SIGINT')).toBe(0);
    expect(signalTarget.listenerCount('SIGTERM')).toBe(0);
  });
});
