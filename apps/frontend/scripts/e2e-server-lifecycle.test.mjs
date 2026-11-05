import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import { createE2EServerLifecycle, unexpectedExitCode } from './e2e-server-lifecycle.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function stoppedApp(stop = vi.fn(async () => {})) {
  return { child: new EventEmitter(), stop, terminated: false };
}

function idleProxy() {
  const proxy = new EventEmitter();
  proxy.listening = false;
  proxy.listen = vi.fn();
  proxy.closeAllConnections = vi.fn();
  proxy.close = vi.fn();
  return proxy;
}

describe('E2E server lifecycle', () => {
  it('maps an early successful child exit to failure after cleanup and refuses late resources', async () => {
    const stopGate = deferred();
    const app = stoppedApp(vi.fn(() => stopGate.promise));
    const parent = { exitCode: undefined };
    const lifecycle = createE2EServerLifecycle({
      app,
      onUnexpectedAppExit(code) {
        parent.exitCode = unexpectedExitCode(code);
      },
    });

    app.child.emit('exit', 0);
    await vi.waitFor(() => expect(app.stop).toHaveBeenCalledOnce());
    expect(parent.exitCode).toBeUndefined();
    expect(lifecycle.stop()).toBe(lifecycle.stop());
    expect(app.child.listenerCount('exit')).toBe(0);

    stopGate.resolve();
    await vi.waitFor(() => expect(parent.exitCode).toBe(1));
    expect(unexpectedExitCode(null)).toBe(1);
    expect(unexpectedExitCode(9)).toBe(9);

    const proxy = idleProxy();
    const fixture = { close: vi.fn() };
    await expect(lifecycle.listen({ proxy, fixture, port: 4_321 })).rejects.toThrow(
      'stopped before the E2E proxy could listen',
    );
    expect(proxy.listen).not.toHaveBeenCalled();
    expect(proxy.close).not.toHaveBeenCalled();
    expect(fixture.close).toHaveBeenCalledOnce();
    expect(app.stop).toHaveBeenCalledOnce();
  });

  it('cleans a proxy that finishes binding after the standalone child exits', async () => {
    const app = stoppedApp();
    const parent = { exitCode: undefined };
    const proxy = idleProxy();
    proxy.listen.mockImplementation(() => {});
    proxy.closeAllConnections.mockImplementation(() => {});
    proxy.close.mockImplementation((callback) => {
      proxy.listening = false;
      callback();
    });
    const fixture = { close: vi.fn() };
    const lifecycle = createE2EServerLifecycle({
      app,
      onUnexpectedAppExit(code) {
        parent.exitCode = unexpectedExitCode(code);
      },
    });
    const listening = lifecycle.listen({ proxy, fixture, port: 4_321 });

    app.child.emit('exit', 0);
    await Promise.resolve();
    expect(app.stop).not.toHaveBeenCalled();

    proxy.listening = true;
    proxy.emit('listening');
    await expect(listening).rejects.toThrow('exited while the E2E proxy was starting');

    expect(fixture.close).toHaveBeenCalledOnce();
    expect(proxy.closeAllConnections).toHaveBeenCalledOnce();
    expect(proxy.close).toHaveBeenCalledOnce();
    expect(app.stop).toHaveBeenCalledOnce();
    expect(parent.exitCode).toBe(1);
    expect(proxy.listenerCount('error')).toBe(0);
    expect(proxy.listenerCount('listening')).toBe(0);
    expect(app.child.listenerCount('exit')).toBe(0);
  });

  it('rethrows the exact proxy bind error after successful cleanup', async () => {
    const bindError = Object.assign(new Error('address already in use'), { code: 'EADDRINUSE' });
    const proxy = idleProxy();
    proxy.listen.mockImplementation(() => queueMicrotask(() => proxy.emit('error', bindError)));
    const fixture = { close: vi.fn() };
    const app = stoppedApp();
    const lifecycle = createE2EServerLifecycle({ app });

    await expect(lifecycle.listen({ proxy, fixture, port: 4_321 })).rejects.toBe(bindError);
    expect(fixture.close).toHaveBeenCalledOnce();
    expect(proxy.closeAllConnections).not.toHaveBeenCalled();
    expect(proxy.close).not.toHaveBeenCalled();
    expect(app.stop).toHaveBeenCalledOnce();
  });

  it('awaits child cleanup and preserves a proxy bind error', async () => {
    const bindError = Object.assign(new Error('address already in use'), { code: 'EADDRINUSE' });
    const fixtureError = new Error('fixture cleanup failed');
    const proxy = idleProxy();
    proxy.listen.mockImplementation(() => queueMicrotask(() => proxy.emit('error', bindError)));
    proxy.closeAllConnections.mockImplementation(() => {
      throw new Error('must not close a proxy that never listened');
    });
    proxy.close.mockImplementation(() => {
      throw new Error('must not close a proxy that never listened');
    });
    const fixture = {
      close: vi.fn(() => {
        throw fixtureError;
      }),
    };
    const stopGate = deferred();
    const app = stoppedApp(vi.fn(() => stopGate.promise));
    const onUnexpectedAppExit = vi.fn();
    const lifecycle = createE2EServerLifecycle({ app, onUnexpectedAppExit });
    let settled = false;
    const outcome = lifecycle.listen({ proxy, fixture, port: 4_321 }).then(
      () => {
        settled = true;
        return null;
      },
      (error) => {
        settled = true;
        return error;
      },
    );

    await vi.waitFor(() => expect(app.stop).toHaveBeenCalledOnce());
    expect(settled).toBe(false);
    expect(lifecycle.stop()).toBe(lifecycle.stop());
    expect(fixture.close).toHaveBeenCalledOnce();
    expect(proxy.closeAllConnections).not.toHaveBeenCalled();
    expect(proxy.close).not.toHaveBeenCalled();
    expect(proxy.listenerCount('error')).toBe(0);
    expect(proxy.listenerCount('listening')).toBe(0);
    expect(app.child.listenerCount('exit')).toBe(0);

    stopGate.resolve();
    const error = await outcome;
    expect(error).toBeInstanceOf(AggregateError);
    expect(error.cause).toBe(bindError);
    expect(error.errors).toEqual([bindError, fixtureError]);
    expect(app.stop).toHaveBeenCalledOnce();
    expect(onUnexpectedAppExit).not.toHaveBeenCalled();
  });
});
