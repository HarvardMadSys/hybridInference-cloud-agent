#!/usr/bin/env node
//
// P1's acceptance, executed rather than asserted in prose: build once, serve
// the *same* standalone artifact twice under two different runtime
// environments, and prove the rendered page follows the environment.
//
// A unit test cannot show this. The failure being guarded against is a value
// that is resolved during `next build` and frozen into the bundle, and the
// only way to see it is to boot the built server and read what it serves. A
// suite that imported the layout would pass against a build-time read just as
// happily as a runtime one.
//
// Four things are checked:
//
//   1. Two environments produce two different documents from one artifact.
//   2. Each document carries its own deployment's name, not the other's and
//      not a neutral default.
//   3. A secret-looking variable in the environment never reaches the HTML or
//      the client bundle. The config reader is an allowlist; this is what
//      proves a field that is not on it cannot be published.
//   4. An illegal value fails the request instead of being read as "unset".
//      Silent fallback is what this work removed; failing loud is the contract.
//
// Run from `web/` after `npm run build`.

import { spawn } from 'node:child_process';
import { cpSync, existsSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const WEB_ROOT = resolve(import.meta.dirname, '..');
const STANDALONE = join(WEB_ROOT, '.next', 'standalone');
const SERVER = join(STANDALONE, 'server.js');
const STATIC_DIR = join(WEB_ROOT, '.next', 'static');
const SENTINEL = 'sentinel-runtime-config-must-not-leak';

const STARTUP_TIMEOUT_MS = 60_000;
const POLL_INTERVAL_MS = 500;

function fail(message) {
  console.error(`::error title=Runtime site config::${message}`);
  process.exit(1);
}

/**
 * Mirror what the runtime image does with the standalone output.
 *
 * `next build` emits the static assets outside `.next/standalone`, and the
 * server looks for them inside its own directory. The Dockerfile copies them
 * across; doing the same here means this check exercises the same layout the
 * image ships, rather than a shape only the test has.
 */
function prepareStandalone() {
  if (!existsSync(SERVER)) {
    fail(`no standalone server at ${SERVER}; run \`npm run build\` first`);
  }
  if (existsSync(STATIC_DIR)) {
    cpSync(STATIC_DIR, join(STANDALONE, '.next', 'static'), { recursive: true });
  }
}

function startServer(port, extraEnv) {
  const child = spawn(process.execPath, [SERVER], {
    cwd: STANDALONE,
    env: {
      ...process.env,
      NODE_ENV: 'production',
      HOSTNAME: '127.0.0.1',
      PORT: String(port),
      ...extraEnv,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const logs = [];
  child.stdout.on('data', (chunk) => logs.push(String(chunk)));
  child.stderr.on('data', (chunk) => logs.push(String(chunk)));
  child.on('error', (error) => logs.push(`spawn error: ${error.message}`));

  return {
    child,
    logs,
    stop() {
      return new Promise((done) => {
        if (child.exitCode !== null || child.signalCode !== null) return done();
        child.once('exit', () => done());
        child.kill('SIGTERM');
        // The server closes its listener on SIGTERM, but a stuck process must
        // not hang CI.
        setTimeout(() => child.kill('SIGKILL'), 5_000).unref();
      });
    },
  };
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForResponse(port) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let lastError = 'no attempt made';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/login`);
      return response;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      await sleep(POLL_INTERVAL_MS);
    }
  }
  throw new Error(`server on port ${port} did not answer within ${STARTUP_TIMEOUT_MS}ms: ${lastError}`);
}

function titleOf(html) {
  const match = html.match(/<title[^>]*>([^<]*)<\/title>/i);
  return match ? match[1].trim() : null;
}

/** Every JavaScript chunk the browser would download for this build. */
function clientBundle() {
  const chunks = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (entry.endsWith('.js')) chunks.push(readFileSync(path, 'utf8'));
    }
  };
  if (existsSync(STATIC_DIR)) walk(STATIC_DIR);
  return chunks.join('\n');
}

async function renderWith(port, env) {
  const server = startServer(port, env);
  try {
    const response = await waitForResponse(port);
    const html = await response.text();
    return { status: response.status, html, title: titleOf(html), logs: server.logs.join('') };
  } finally {
    await server.stop();
  }
}

const ALPHA = {
  AGENT_WEB_APP_NAME: 'Alpha Agents',
  AGENT_WEB_ORG_NAME: 'Alpha Research',
  AGENT_WEB_ORG_URL: 'https://alpha.example',
  AGENT_WEB_STATUS_URL: 'https://status.alpha.example/',
  AGENT_WEB_GATEWAY_URL: 'https://gateway-alpha.example/authorize',
  AGENT_WEB_FEATURE_RAG: 'true',
  // Not a configuration variable. It is here to prove that an unrelated
  // environment value cannot ride along with the allowlisted ones.
  AGENT_SESSION_SECRET: SENTINEL,
};

const BETA = {
  AGENT_WEB_APP_NAME: 'Beta Agents',
  AGENT_WEB_ORG_NAME: 'Beta Research',
  AGENT_WEB_ORG_URL: 'https://beta.example',
  AGENT_WEB_STATUS_URL: 'https://status.beta.example/',
  AGENT_WEB_GATEWAY_URL: 'https://gateway-beta.example',
  AGENT_WEB_FEATURE_RAG: 'false',
  AGENT_SESSION_SECRET: SENTINEL,
};

async function main() {
  prepareStandalone();

  const alpha = await renderWith(3311, ALPHA);
  if (alpha.status !== 200) {
    fail(`the alpha deployment answered ${alpha.status}\n${alpha.logs}`);
  }
  if (alpha.title !== ALPHA.AGENT_WEB_APP_NAME) {
    fail(`expected title "${ALPHA.AGENT_WEB_APP_NAME}", got ${JSON.stringify(alpha.title)}`);
  }

  const beta = await renderWith(3312, BETA);
  if (beta.status !== 200) {
    fail(`the beta deployment answered ${beta.status}\n${beta.logs}`);
  }
  if (beta.title !== BETA.AGENT_WEB_APP_NAME) {
    fail(`expected title "${BETA.AGENT_WEB_APP_NAME}", got ${JSON.stringify(beta.title)}`);
  }

  // The core assertion. Equal documents from one artifact mean the value was
  // resolved at build time, whatever the unit tests say.
  if (alpha.title === beta.title || alpha.html === beta.html) {
    fail(
      'one built artifact rendered the same document under two runtime ' +
        'environments — the configuration is still being resolved at build time',
    );
  }

  // A component rendered outside the provider must not fall back to a real
  // deployment's brand.
  for (const [name, rendered] of [
    ['alpha', alpha],
    ['beta', beta],
  ]) {
    if (!rendered.html.includes(rendered.title)) {
      fail(`the ${name} document does not contain its own configured name`);
    }
    if (rendered.html.includes(SENTINEL)) {
      fail(`the ${name} HTML contains a variable that is not on the allowlist`);
    }
  }

  const bundle = clientBundle();
  if (bundle.includes(SENTINEL)) {
    fail('a variable that is not on the allowlist reached the client bundle');
  }
  for (const name of [ALPHA.AGENT_WEB_APP_NAME, BETA.AGENT_WEB_APP_NAME]) {
    if (bundle.includes(name)) {
      fail(`the deployment name "${name}" is baked into the client bundle`);
    }
  }

  // An illegal value must fail the request. Reading it as "unset" is the
  // silent-fallback behaviour this work removed. Asserted as "not a normal
  // render" rather than "exactly 500": the point is that the page does not
  // quietly serve the deployment it was configured to be. The message that
  // names the variable is pinned by the unit tests, where it is not at the
  // mercy of how Next surfaces a server error.
  const broken = await renderWith(3313, { ...ALPHA, AGENT_WEB_STATUS_URL: 'not-a-url' });
  if (broken.status === 200 && broken.title === ALPHA.AGENT_WEB_APP_NAME) {
    fail(
      'an invalid AGENT_WEB_STATUS_URL rendered a normal page; it must fail ' +
        'rather than be read as unset',
    );
  }

  console.log('Runtime site config: one artifact, two deployments, both correct.');
}

// A leftover copy from a previous run would mask a broken Dockerfile copy
// step, so start from a clean standalone tree.
rmSync(join(STANDALONE, '.next', 'static'), { recursive: true, force: true });

await main();
