class RuntimeSiteConfigError extends Error {}

export function fail(message) {
  console.error(`::error title=Runtime site config::${message}`);
  throw new RuntimeSiteConfigError(message);
}

function closeControlPlane(controlPlane) {
  return new Promise((resolve, reject) =>
    controlPlane.close((error) => (error ? reject(error) : resolve())),
  );
}

/** Start the app lazily and settle both owned servers before reporting failure. */
export async function withRuntimeResources(
  { controlPlane, startServer = async () => undefined },
  operation,
) {
  let server;
  let value;
  let operationError;
  try {
    server = await startServer();
    value = await operation(server);
  } catch (error) {
    operationError = error;
  }

  const cleanupResults = await Promise.allSettled([
    Promise.resolve().then(() => server?.stop()),
    Promise.resolve().then(() => closeControlPlane(controlPlane)),
  ]);
  const cleanupErrors = cleanupResults
    .filter((result) => result.status === 'rejected')
    .map((result) => result.reason);

  if (operationError && cleanupErrors.length > 0) {
    throw new AggregateError(
      [operationError, ...cleanupErrors],
      'Runtime site config assertion and cleanup failed',
      { cause: operationError },
    );
  }
  if (operationError) throw operationError;
  if (cleanupErrors.length === 1) throw cleanupErrors[0];
  if (cleanupErrors.length > 1) {
    throw new AggregateError(cleanupErrors, 'Runtime site config cleanup failed');
  }
  return value;
}

/** Convert an assertion failure into the script's single non-zero exit path. */
export async function runRuntimeGuard(operation) {
  try {
    await operation();
  } catch (error) {
    if (!(error instanceof RuntimeSiteConfigError)) console.error(error);
    process.exitCode = 1;
  }
}
