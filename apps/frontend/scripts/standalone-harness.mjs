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

function identityOf(variant) {
  return {
    name: variant.name,
    basePath: variant.basePath,
    apiUrl: variant.apiUrl,
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

/** Build and identify one production artifact. The caller owns environment policy. */
export function buildStandalone(variant, { env, stdio = 'inherit' } = {}) {
  rmSync(NEXT_DIR, { recursive: true, force: true });
  const result = spawnSync(process.execPath, [NEXT_BIN, 'build'], {
    cwd: WEB_ROOT,
    env: env ?? process.env,
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

/** Start the staged production server. The caller owns its runtime environment. */
export async function startStandalone({ env, captureOutput = false } = {}) {
  const port = await reservePort();
  const logs = [];
  const child = spawn(process.execPath, [STANDALONE_SERVER], {
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
  child.on('error', (error) => logs.push(`spawn error: ${error.message}`));

  let stopping = false;
  return {
    child,
    logs,
    port,
    async stop() {
      if (stopping || child.exitCode !== null || child.signalCode !== null) return;
      stopping = true;
      await new Promise((resolve) => {
        const killTimer = setTimeout(() => child.kill('SIGKILL'), 5_000);
        killTimer.unref();
        child.once('exit', () => {
          clearTimeout(killTimer);
          resolve();
        });
        child.kill('SIGTERM');
      });
    },
  };
}

export async function waitForResponse(
  url,
  { server, timeoutMs = 60_000, pollIntervalMs = 100, acceptResponse = () => true } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'no attempt made';
  while (Date.now() < deadline) {
    if (server && (server.child.exitCode !== null || server.child.signalCode !== null)) {
      throw new Error(
        `Production server exited before ${url} answered${server.logs.length ? `:\n${server.logs.join('')}` : ''}`,
      );
    }
    try {
      const response = await fetch(url);
      if (acceptResponse(response)) return response;
      lastError = `answered ${response.status}`;
      await response.arrayBuffer();
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  throw new Error(`${url} did not answer within ${timeoutMs}ms: ${lastError}`);
}
