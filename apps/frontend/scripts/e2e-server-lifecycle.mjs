function closeProxy(proxy) {
  return new Promise((resolve, reject) => {
    proxy.close((error) => (error ? reject(error) : resolve()));
  });
}

function prepareListening(proxy) {
  let resolveListening;
  let rejectListening;
  const promise = new Promise((resolve, reject) => {
    resolveListening = resolve;
    rejectListening = reject;
  });
  let settled = false;
  const finish = (callback, value) => {
    if (settled) return;
    settled = true;
    proxy.removeListener('error', onError);
    proxy.removeListener('listening', onListening);
    callback(value);
  };
  const onError = (error) => finish(rejectListening, error);
  const onListening = () => finish(resolveListening);
  proxy.once('error', onError);
  proxy.once('listening', onListening);
  return {
    promise,
    start(port, hostname) {
      try {
        proxy.listen(port, hostname);
      } catch (error) {
        onError(error);
      }
    },
  };
}

function cleanupFailures(error) {
  return error instanceof AggregateError ? error.errors : [error];
}

function throwCleanupFailures(failures) {
  if (failures.length === 1) throw failures[0];
  if (failures.length > 1) throw new AggregateError(failures, 'E2E server cleanup failed');
}

export function unexpectedExitCode(code) {
  return typeof code === 'number' && code !== 0 ? code : 1;
}

/** Own the standalone child immediately, then any browser resources passed to listen. */
export function createE2EServerLifecycle({ app, onUnexpectedAppExit = () => {} }) {
  let proxy;
  let fixture;
  let resourcesCleaned = false;
  let bindingPromise;
  let stopPromise;
  let exitObserved = false;

  async function cleanupOwnedResources() {
    if (resourcesCleaned || (!proxy && !fixture)) return [];
    resourcesCleaned = true;
    const failures = [];
    try {
      fixture?.close();
    } catch (error) {
      failures.push(error);
    }
    if (proxy?.listening) {
      try {
        proxy.closeAllConnections();
      } catch (error) {
        failures.push(error);
      }
      try {
        await closeProxy(proxy);
      } catch (error) {
        failures.push(error);
      }
    }
    return failures;
  }

  function stop() {
    if (stopPromise) return stopPromise;
    stopPromise = (async () => {
      app.child.removeListener('exit', onAppExit);
      if (bindingPromise) {
        try {
          await bindingPromise;
        } catch {
          // The listen caller preserves the bind error; cleanup must still run.
        }
      }
      const failures = await cleanupOwnedResources();
      try {
        await app.stop();
      } catch (error) {
        failures.push(error);
      }
      throwCleanupFailures(failures);
    })();
    return stopPromise;
  }

  const onAppExit = (code) => {
    if (exitObserved) return;
    exitObserved = true;
    void stop().then(
      () => onUnexpectedAppExit(code, null),
      (error) => onUnexpectedAppExit(code, error),
    );
  };
  app.child.once('exit', onAppExit);
  if (app.terminated) queueMicrotask(() => onAppExit(app.child.exitCode));

  async function refuseLateResources() {
    const refused = new Error('The standalone app stopped before the E2E proxy could listen');
    const failures = await cleanupOwnedResources();
    try {
      await stopPromise;
    } catch (error) {
      failures.push(...cleanupFailures(error));
    }
    if (failures.length > 0) {
      throw new AggregateError([refused, ...failures], refused.message, { cause: refused });
    }
    throw refused;
  }

  async function listen({ proxy: nextProxy, fixture: nextFixture, port, hostname = '127.0.0.1' }) {
    if (proxy || fixture) throw new Error('The E2E proxy lifecycle can only listen once');
    proxy = nextProxy;
    fixture = nextFixture;
    if (stopPromise) return refuseLateResources();

    const binding = prepareListening(proxy);
    bindingPromise = binding.promise;
    binding.start(port, hostname);
    try {
      await bindingPromise;
    } catch (bindError) {
      try {
        await stop();
      } catch (cleanupError) {
        throw new AggregateError(
          [bindError, ...cleanupFailures(cleanupError)],
          'E2E proxy could not listen and cleanup failed',
          { cause: bindError },
        );
      }
      throw bindError;
    }

    if (stopPromise) {
      await stopPromise;
      throw new Error('The standalone app exited while the E2E proxy was starting');
    }
  }

  return { listen, stop };
}
