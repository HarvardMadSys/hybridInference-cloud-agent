import { spawn, spawnSync } from 'node:child_process';
import { cpSync, existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer as createPortReservation } from 'node:net';
import { join, resolve } from 'node:path';

export const WEB_ROOT = resolve(import.meta.dirname, '..');
export const NEXT_DIR = join(WEB_ROOT, '.next');
export const STANDALONE_DIR = join(NEXT_DIR, 'standalone');
export const STANDALONE_SERVER = join(STANDALONE_DIR, 'server.js');
export const STATIC_DIR = join(NEXT_DIR, 'static');

const NEXT_BIN = join(WEB_ROOT, 'node_modules', 'next', 'dist', 'bin', 'next');
const ARTIFACT_IDENTITY = join(NEXT_DIR, 'cloud-agent-web-variant.json');
const STOP_GRACE_MS = 5_000;
const FORCE_KILL_WAIT_MS = 1_000;
const SHUTDOWN_SIGNALS = ['SIGINT', 'SIGTERM'];
const RUNTIME_SITE_ENV_KEYS = new Set(['AGENT_CONTROL_PLANE_URL', 'AGENT_DEPLOYMENT_SHA']);

function identityOf(variant) {
  return {
    name: variant.name,
    basePath: variant.basePath,
    apiUrl: variant.apiUrl,
  };
}

/** Remove deployment identity that must only come from the selected fixture. */
export function sanitizeRuntimeSiteEnvironment(env) {
  return Object.fromEntries(
    Object.entries(env).filter(
      ([key]) => !key.startsWith('AGENT_WEB_') && !RUNTIME_SITE_ENV_KEYS.has(key),
    ),
  );
}

/** Bind a build's public paths to the identity written beside its artifact. */
export function buildEnvironmentForVariant(variant, env = process.env) {
  return {
    ...env,
    NEXT_PUBLIC_BASE_PATH: variant.basePath,
    NEXT_PUBLIC_AGENT_API_URL: variant.apiUrl,
  };
}

/** Stage exactly the files copied by deploy/control-plane/Dockerfile.web. */
export function stageStandalone() {
  if (!existsSync(STANDALONE_SERVER)) {
    throw new Error(`No standalone server at ${STANDALONE_SERVER} after the build`);
  }
  if (!existsSync(STATIC_DIR)) {
    throw new Error(`No static asset directory at ${STATIC_DIR} after the build`);
  }
  const destination = join(STANDALONE_DIR, '.next', 'static');
  rmSync(destination, { recursive: true, force: true });
  cpSync(STATIC_DIR, destination, { recursive: true });
}

/** Build and identify one production artifact. The variant owns its public paths. */
export function buildStandalone(variant, { env, stdio = 'inherit' } = {}) {
  rmSync(NEXT_DIR, { recursive: true, force: true });
  const result = spawnSync(process.execPath, [NEXT_BIN, 'build'], {
    cwd: WEB_ROOT,
    env: buildEnvironmentForVariant(variant, env),
    encoding: stdio === 'inherit' ? undefined : 'utf8',
    stdio,
  });
  if (result.error) {
    throw new Error(`Could not run \`next build\` for ${variant.name}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const output = stdio === 'inherit' ? '' : `\n${(result.stdout ?? '') + (result.stderr ?? '')}`;
    throw new Error(`\`next build\` failed for ${variant.name}${output}`);
  }

  stageStandalone();
  writeFileSync(ARTIFACT_IDENTITY, `${JSON.stringify(identityOf(variant), null, 2)}\n`);
  verifyStandaloneVariant(variant);
}

/** Refuse to reuse a successful build produced for another mount or API path. */
export function verifyStandaloneVariant(variant) {
  if (!existsSync(STANDALONE_SERVER)) {
    throw new Error(`No reusable standalone server at ${STANDALONE_SERVER}`);
  }
  if (!existsSync(join(STANDALONE_DIR, '.next', 'static'))) {
    throw new Error('The reusable standalone artifact has not been staged');
  }

  let actual;
  try {
    actual = JSON.parse(readFileSync(ARTIFACT_IDENTITY, 'utf8'));
  } catch (error) {
    throw new Error(`The reusable artifact has no valid variant identity: ${error.message}`);
  }
  if (JSON.stringify(actual) !== JSON.stringify(identityOf(variant))) {
    throw new Error(
      `Expected ${JSON.stringify(identityOf(variant))}, found reusable artifact ${JSON.stringify(actual)}`,
    );
  }

  const routes = JSON.parse(readFileSync(join(NEXT_DIR, 'routes-manifest.json'), 'utf8'));
  if (routes.basePath !== variant.basePath) {
    throw new Error(
      `Expected basePath ${JSON.stringify(variant.basePath)}, found ${JSON.stringify(routes.basePath)}`,
    );
  }
}

export async function reservePort() {
  const reservation = createPortReservation();
  await new Promise((resolve, reject) => {
    reservation.once('error', reject);
    reservation.listen(0, '127.0.0.1', resolve);
  });
  const address = reservation.address();
  if (!address || typeof address === 'string') {
    reservation.close();
    throw new Error('Could not reserve a loopback port');
  }
  await new Promise((resolve, reject) =>
    reservation.close((error) => (error ? reject(error) : resolve())),
  );
  return address.port;
}

function waitForTermination(termination, timeoutMs) {
  return new Promise((resolve) => {
    let finished = false;
    const finish = (terminated) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(terminated);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    timer.unref?.();
    void termination.then(() => finish(true));
  });
}

/** Start the staged production server. The caller owns its runtime environment. */
export async function startStandalone({
  env,
  captureOutput = false,
  spawnProcess = spawn,
  reserveServerPort = reservePort,
  signalTarget = process,
  stopGraceMs = STOP_GRACE_MS,
  forceKillWaitMs = FORCE_KILL_WAIT_MS,
} = {}) {
  const port = await reserveServerPort();
  const logs = [];
  const child = spawnProcess(process.execPath, [STANDALONE_SERVER], {
    cwd: STANDALONE_DIR,
    env: {
      ...(env ?? process.env),
      NODE_ENV: 'production',
      HOSTNAME: '127.0.0.1',
      PORT: String(port),
    },
    stdio: captureOutput ? ['ignore', 'pipe', 'pipe'] : 'inherit',
  });
  if (captureOutput) {
    child.stdout.on('data', (chunk) => logs.push(String(chunk)));
    child.stderr.on('data', (chunk) => logs.push(String(chunk)));
  }
  let terminated = child.exitCode !== null || child.signalCode !== null;
  let resolveTermination;
  const termination = new Promise((resolve) => {
    resolveTermination = resolve;
    if (terminated) resolve();
  });
  let removeSignalHandlers = () => {};
  const markTerminated = () => {
    if (terminated) return;
    terminated = true;
    removeSignalHandlers();
    resolveTermination();
  };
  child.once('exit', markTerminated);
  child.on('error', (error) => {
    logs.push(`spawn error: ${error.message}`);
    // A failed spawn has no exit event. Later ChildProcess errors do not prove
    // that an already-running process has stopped.
    if (child.pid === undefined) markTerminated();
  });

  let stopPromise;
  const server = {
    child,
    logs,
    port,
    get terminated() {
      return terminated;
    },
    stop() {
      if (stopPromise) return stopPromise;
      stopPromise = (async () => {
        if (terminated) return;
        child.kill('SIGTERM');
        if (await waitForTermination(termination, stopGraceMs)) return;
        child.kill('SIGKILL');
        if (await waitForTermination(termination, forceKillWaitMs)) return;
        throw new Error(
          `Production server did not exit after SIGKILL${logs.length ? `:\n${logs.join('')}` : ''}`,
        );
      })().finally(removeSignalHandlers);
      return stopPromise;
    },
  };

  let forwardedSignal = null;
  const signalHandlers = new Map(
    SHUTDOWN_SIGNALS.map((signal) => [
      signal,
      () => {
        if (forwardedSignal) return;
        forwardedSignal = signal;
        void server.stop().then(
          () => signalTarget.kill(signalTarget.pid, signal),
          (error) => {
            logs.push(`shutdown error: ${error.message}`);
            signalTarget.kill(signalTarget.pid, signal);
          },
        );
      },
    ]),
  );
  removeSignalHandlers = () => {
    for (const [signal, handler] of signalHandlers) {
      signalTarget.removeListener(signal, handler);
    }
  };
  for (const [signal, handler] of signalHandlers) signalTarget.on(signal, handler);
  if (terminated) removeSignalHandlers();

  return server;
}

export async function waitForResponse(
  url,
  { server, timeoutMs = 60_000, pollIntervalMs = 100, acceptResponse = () => true } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'no attempt made';
  while (Date.now() < deadline) {
    if (
      server &&
      (server.terminated || server.child.exitCode !== null || server.child.signalCode !== null)
    ) {
      throw new Error(
        `Production server exited before ${url} answered${server.logs.length ? `:\n${server.logs.join('')}` : ''}`,
      );
    }
    const controller = new AbortController();
    const attemptTimeoutMs = Math.max(1, deadline - Date.now());
    const abortTimer = setTimeout(() => controller.abort(), attemptTimeoutMs);
    abortTimer.unref?.();
    let responseDiagnostic = null;
    try {
      const response = await fetch(url, { signal: controller.signal });
      responseDiagnostic = `answered ${response.status}`;
      lastError = responseDiagnostic;
      const accepted = acceptResponse(response);
      const body = await response.arrayBuffer();
      if (accepted) {
        return new Response(body.byteLength === 0 ? null : body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        });
      }
    } catch (error) {
      if (controller.signal.aborted) {
        lastError = responseDiagnostic
          ? `${responseDiagnostic}; response did not complete before the deadline`
          : 'request did not complete before the deadline';
      } else {
        lastError = error instanceof Error ? error.message : String(error);
      }
    } finally {
      clearTimeout(abortTimer);
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(pollIntervalMs, remainingMs)));
    }
  }
  throw new Error(
    `${url} did not answer within ${timeoutMs}ms: ${lastError}${
      server?.logs.length ? `\n${server.logs.join('')}` : ''
    }`,
  );
}
