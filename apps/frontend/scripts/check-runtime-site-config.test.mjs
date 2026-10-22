import { afterEach, describe, expect, it, vi } from 'vitest';
import { fail, withRuntimeResources } from './check-runtime-site-config.mjs';

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function gatedControlPlane() {
  let finish;
  const controlPlane = {
    close: vi.fn((callback) => {
      finish = callback;
    }),
  };
  return { controlPlane, finish: () => finish() };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('runtime site config cleanup', () => {
  it('settles the server and control plane before reporting an assertion', async () => {
    const serverGate = deferred();
    const server = { stop: vi.fn(() => serverGate.promise) };
    const control = gatedControlPlane();
    const reported = vi.spyOn(console, 'error').mockImplementation(() => {});
    let settled = false;
    const outcome = withRuntimeResources(
      { controlPlane: control.controlPlane, startServer: async () => server },
      async () => {
        fail('injected assertion failure');
      },
    ).then(
      () => {
        settled = true;
        return null;
      },
      (error) => {
        settled = true;
        return error;
      },
    );

    await vi.waitFor(() => {
      expect(server.stop).toHaveBeenCalledOnce();
      expect(control.controlPlane.close).toHaveBeenCalledOnce();
    });
    expect(settled).toBe(false);

    serverGate.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);

    control.finish();
    const error = await outcome;
    expect(error).toMatchObject({ message: 'injected assertion failure' });
    expect(reported).toHaveBeenCalledWith(
      '::error title=Runtime site config::injected assertion failure',
    );
    expect(server.stop).toHaveBeenCalledOnce();
    expect(control.controlPlane.close).toHaveBeenCalledOnce();
  });

  it('closes the control plane when lazy server startup rejects', async () => {
    const startError = new Error('injected server start failure');
    const control = gatedControlPlane();
    const operation = vi.fn();
    const startServer = vi.fn(async () => {
      throw startError;
    });
    let settled = false;
    const outcome = withRuntimeResources(
      { controlPlane: control.controlPlane, startServer },
      operation,
    ).then(
      () => {
        settled = true;
        return null;
      },
      (error) => {
        settled = true;
        return error;
      },
    );

    await vi.waitFor(() => expect(control.controlPlane.close).toHaveBeenCalledOnce());
    expect(settled).toBe(false);
    expect(operation).not.toHaveBeenCalled();

    control.finish();
    expect(await outcome).toBe(startError);
    expect(startServer).toHaveBeenCalledOnce();
  });

  it('awaits control-plane cleanup even when server cleanup rejects', async () => {
    const stopError = new Error('injected server stop failure');
    const server = {
      stop: vi.fn(async () => {
        throw stopError;
      }),
    };
    const control = gatedControlPlane();
    let settled = false;
    const outcome = withRuntimeResources(
      { controlPlane: control.controlPlane, startServer: async () => server },
      async () => 'unused',
    ).then(
      () => {
        settled = true;
        return null;
      },
      (error) => {
        settled = true;
        return error;
      },
    );

    await vi.waitFor(() => {
      expect(server.stop).toHaveBeenCalledOnce();
      expect(control.controlPlane.close).toHaveBeenCalledOnce();
    });
    expect(settled).toBe(false);

    control.finish();
    expect(await outcome).toBe(stopError);
    expect(control.controlPlane.close).toHaveBeenCalledOnce();
  });
});
