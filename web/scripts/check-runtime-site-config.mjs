#!/usr/bin/env node
//
// The acceptance for "one artifact, many deployments", executed rather than
// asserted in prose: build each web variant once, serve the *same* artifact
// under two different runtime environments, and read what it actually renders.
//
// A unit test cannot show this. The failure being guarded against is a value
// resolved during `next build` and frozen into the bundle, and the only way to
// see it is to boot the built server. A suite that imported the layout would
// pass against a build-time read exactly as happily as a runtime one.
//
// Two variants are built, because they are two images and the paths differ:
//
//   web-root    basePath ""        browser API /api         (own hostname)
//   web-agents  basePath "/agents" browser API /agents/api  (gateway hostname)
//
// For each variant and each of two deployments, this checks:
//
//   1. the document is served at the variant's own path, and not at a bare
//      path when the variant is mounted under a prefix;
//   2. `<title>` and the rendered `<h1>` are the *configured* name — the body,
//      not only the head, so a document that merely echoes its own <title>
//      cannot pass;
//   3. the sign-in link and the asset URLs carry the variant's prefix. These
//      are the two things a wrongly-built variant gets wrong, and the sign-in
//      URL is the one string this app hands to another service;
//   4. every configured identity value — name, organisation, organisation
//      URL, status URL, gateway origin — reaches the document, and the
//      feature switch has the configured value;
//   5. no value from the *other* deployment appears anywhere, which is what a
//      fallback to a build-time constant looks like;
//   6. a secret-looking variable in the environment reaches neither the HTML
//      nor the client bundle, and no deployment value is baked into the
//      bundle at all;
//   7. an illegal value fails the request instead of being read as "unset".
//
// Run from `web/`.

import { spawn, spawnSync } from 'node:child_process';
import { cpSync, existsSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const WEB_ROOT = resolve(import.meta.dirname, '..');
const NEXT_BIN = join(WEB_ROOT, 'node_modules', 'next', 'dist', 'bin', 'next');
const NEXT_DIR = join(WEB_ROOT, '.next');
const STANDALONE = join(NEXT_DIR, 'standalone');
const SERVER = join(STANDALONE, 'server.js');
const STATIC_DIR = join(NEXT_DIR, 'static');

const SENTINEL = 'sentinel-runtime-config-must-not-leak';
const STARTUP_TIMEOUT_MS = 60_000;
const POLL_INTERVAL_MS = 400;

let nextPort = 3411;

const VARIANTS = [
  { name: 'web-root', basePath: '', apiUrl: '/api', foreignPaths: [] },
  {
    name: 'web-agents',
    basePath: '/agents',
    apiUrl: '/agents/api',
    // Mounted under a prefix, the app must not answer at the bare path: that
    // would be a second, unprefixed copy of the same routes.
    foreignPaths: ['/login'],
  },
];

function deployment(spec) {
  const gateway = new URL(spec.gatewayUrl);
  return {
    ...spec,
    gatewayOrigin: gateway.origin,
    gatewayHost: gateway.host,
    env: {
      AGENT_WEB_APP_NAME: spec.appName,
      AGENT_WEB_ORG_NAME: spec.orgName,
      AGENT_WEB_ORG_URL: spec.orgUrl,
      AGENT_WEB_STATUS_URL: spec.statusUrl,
      AGENT_WEB_GATEWAY_URL: spec.gatewayUrl,
      AGENT_WEB_FEATURE_RAG: String(spec.rag),
      // Not configuration. It is here to prove that an unrelated environment
      // value cannot ride along with the allowlisted ones.
      AGENT_SESSION_SECRET: SENTINEL,
    },
  };
}

const DEPLOYMENTS = [
  deployment({
    name: 'alpha',
    appName: 'Alpha Agents',
    orgName: 'Alpha Research',
    orgUrl: 'https://alpha.example',
    statusUrl: 'https://status.alpha.example/',
    gatewayUrl: 'https://gateway-alpha.example/authorize',
    rag: true,
  }),
  deployment({
    name: 'beta',
    appName: 'Beta Agents',
    orgName: 'Beta Research',
    orgUrl: 'https://beta.example',
    statusUrl: 'https://status.beta.example/',
    gatewayUrl: 'https://gateway-beta.example',
    rag: false,
  }),
];

function fail(message) {
  console.error(`::error title=Runtime site config::${message}`);
  process.exit(1);
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function titleOf(html) {
  return firstMatch(html, /<title[^>]*>([^<]*)<\/title>/i);
}

function firstMatch(html, pattern) {
  const match = html.match(pattern);
  return match ? match[1].trim() : null;
}

function textOf(html, tag) {
  const inner = firstMatch(html, new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, 'i'));
  return inner === null ? null : inner.replace(/<[^>]*>/g, '').trim();
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

function buildVariant(variant) {
  // A stale `.next` could serve the previous variant's inlined values, which
  // is exactly the class of mistake this script exists to catch.
  rmSync(NEXT_DIR, { recursive: true, force: true });

  const env = {
    ...process.env,
    NODE_ENV: 'production',
    NEXT_PUBLIC_BASE_PATH: variant.basePath,
    NEXT_PUBLIC_AGENT_API_URL: variant.apiUrl,
  };
  // The build must never see a deployment identity. If it did, a build-time
  // read would look like a runtime one and this whole check would be void.
  for (const key of Object.keys(env)) {
    if (key.startsWith('AGENT_WEB_')) delete env[key];
  }

  const result = spawnSync(process.execPath, [NEXT_BIN, 'build'], {
    cwd: WEB_ROOT,
    env,
    encoding: 'utf8',
  });
  if (result.error) {
    fail(`could not run \`next build\` for ${variant.name}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(
      `\`next build\` failed for ${variant.name}:\n` +
        `${(result.stdout ?? '') + (result.stderr ?? '')}`,
    );
  }

  if (!existsSync(SERVER)) fail(`no standalone server at ${SERVER} after the build`);
  // Mirror what the runtime image does: the static assets are emitted outside
  // `.next/standalone` and have to be copied in.
  rmSync(join(STANDALONE, '.next', 'static'), { recursive: true, force: true });
  cpSync(STATIC_DIR, join(STANDALONE, '.next', 'static'), { recursive: true });
}

function startServer(env, port) {
  const child = spawn(process.execPath, [SERVER], {
    cwd: STANDALONE,
    env: { ...process.env, NODE_ENV: 'production', HOSTNAME: '127.0.0.1', PORT: String(port), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const logs = [];
  child.stdout.on('data', (chunk) => logs.push(String(chunk)));
  child.stderr.on('data', (chunk) => logs.push(String(chunk)));
  child.on('error', (error) => logs.push(`spawn error: ${error.message}`));

  return {
    logs,
    stop() {
      return new Promise((done) => {
        if (child.exitCode !== null || child.signalCode !== null) return done();
        child.once('exit', () => done());
        child.kill('SIGTERM');
        setTimeout(() => child.kill('SIGKILL'), 5_000).unref();
      });
    },
  };
}

/**
 * The first response the server gives, whatever its status.
 *
 * Any status means the process is up; the assertions decide whether the
 * status and body are right. Waiting only for 200 would turn a wrongly-built
 * variant into a timeout, which reads as a broken check rather than a broken
 * build.
 */
async function waitForResponse(url) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let lastError = 'no attempt made';
  while (Date.now() < deadline) {
    try {
      return await fetch(url);
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      await sleep(POLL_INTERVAL_MS);
    }
  }
  throw new Error(`${url} did not answer within ${STARTUP_TIMEOUT_MS}ms: ${lastError}`);
}

async function render(variant, env) {
  const port = nextPort++;
  const server = startServer(env, port);
  try {
    const url = `http://127.0.0.1:${port}${variant.basePath}/login`;
    const response = await waitForResponse(url);
    return { status: response.status, html: await response.text(), logs: server.logs.join('') };
  } finally {
    await server.stop();
  }
}

async function probePath(variant, path) {
  const port = nextPort++;
  const server = startServer(DEPLOYMENTS[0].env, port);
  try {
    const response = await waitForResponse(`http://127.0.0.1:${port}${path}`);
    return response.status;
  } finally {
    await server.stop();
  }
}

function assertRendered(variant, target, rendered) {
  const where = `${variant.name} / ${target.name}`;
  const { html } = rendered;

  if (rendered.status !== 200) {
    fail(`${where}: GET ${variant.basePath}/login answered ${rendered.status}\n${rendered.logs}`);
  }

  const title = titleOf(html);
  if (title !== target.appName) {
    fail(
      `${where}: <title> is ${JSON.stringify(title)}, expected ${JSON.stringify(target.appName)}`,
    );
  }

  // The body, not just the head. Checking `html.includes(title)` would pass on
  // the <title> element alone, with every rendered name and link wrong.
  const heading = textOf(html, 'h1');
  if (heading !== target.appName) {
    fail(
      `${where}: the rendered <h1> is ${JSON.stringify(heading)}, expected the ` +
        `configured name ${JSON.stringify(target.appName)}`,
    );
  }

  // The one URL this app hands to another service, and the string the gateway
  // matches as an exact redirect target.
  const signIn = firstMatch(html, /href="([^"]*\/v1\/session\/login[^"]*)"/);
  const expectedSignIn = `${variant.apiUrl}/v1/session/login`;
  if (!signIn) fail(`${where}: no sign-in link in the document`);
  if (!signIn.startsWith(expectedSignIn)) {
    fail(
      `${where}: the sign-in link is ${JSON.stringify(signIn)}, expected it to ` +
        `start with ${JSON.stringify(expectedSignIn)}`,
    );
  }

  // Assets carry the mount prefix; a /agents deployment that loads the root
  // app's chunks renders unstyled and inert, or 404s outright.
  const asset = firstMatch(html, /(?:src|href)="([^"]*\/_next\/static\/[^"]*)"/);
  if (!asset) fail(`${where}: no asset URL in the document`);
  if (!asset.startsWith(`${variant.basePath}/_next/static/`)) {
    fail(
      `${where}: an asset URL is ${JSON.stringify(asset)}, expected the ` +
        `${variant.basePath || 'root'} prefix`,
    );
  }

  // The identity crosses to the client as serialized props, so all of it must
  // be in the document — not only the two fields this page happens to render.
  for (const [field, value] of [
    ['app name', target.appName],
    ['organisation', target.orgName],
    ['organisation URL', target.orgUrl],
    ['status URL', target.statusUrl],
    ['gateway origin', target.gatewayOrigin],
  ]) {
    if (!html.includes(value)) {
      fail(`${where}: the ${field} ${JSON.stringify(value)} is not in the document`);
    }
  }

  // The feature switch is a boolean, so it is asserted structurally rather
  // than as a substring of the escaped flight payload.
  const unescaped = html.replaceAll('\\"', '"');
  if (!unescaped.includes(`"rag":${target.rag}`)) {
    fail(`${where}: the feature switch is not ${target.rag} in the serialized config`);
  }

  // Anything belonging to the other deployment means a fallback to a
  // build-time constant, which is the whole failure this work removed.
  for (const other of DEPLOYMENTS) {
    if (other.name === target.name) continue;
    for (const [field, value] of [
      ['app name', other.appName],
      ['organisation', other.orgName],
      ['organisation URL', other.orgUrl],
      ['status URL', other.statusUrl],
      ['gateway host', other.gatewayHost],
    ]) {
      if (html.includes(value)) {
        fail(
          `${where}: the document also contains the ${other.name} ${field} ` +
            `${JSON.stringify(value)}`,
        );
      }
    }
  }

  if (html.includes(SENTINEL)) {
    fail(`${where}: a variable that is not on the allowlist reached the HTML`);
  }
}

function assertBundleIsClean(variant) {
  const bundle = clientBundle();
  if (bundle.includes(SENTINEL)) {
    fail(`${variant.name}: a variable that is not on the allowlist reached the client bundle`);
  }
  for (const target of DEPLOYMENTS) {
    for (const [field, value] of [
      ['app name', target.appName],
      ['organisation', target.orgName],
      ['organisation URL', target.orgUrl],
      ['status URL', target.statusUrl],
      ['gateway host', target.gatewayHost],
    ]) {
      if (bundle.includes(value)) {
        fail(
          `${variant.name}: the ${target.name} ${field} ${JSON.stringify(value)} is baked ` +
            'into the client bundle',
        );
      }
    }
  }
}

async function main() {
  for (const variant of VARIANTS) {
    buildVariant(variant);

    for (const target of DEPLOYMENTS) {
      assertRendered(variant, target, await render(variant, target.env));
    }

    // One artifact, two environments, and the two documents must differ — the
    // core assertion. Equal documents mean the value was resolved at build
    // time, whatever the per-field checks above happen to say.
    const alpha = await render(variant, DEPLOYMENTS[0].env);
    const beta = await render(variant, DEPLOYMENTS[1].env);
    if (alpha.html === beta.html) {
      fail(
        `${variant.name}: one artifact rendered the same document under two ` +
          'runtime environments — the configuration is still resolved at build time',
      );
    }

    for (const path of variant.foreignPaths) {
      const status = await probePath(variant, path);
      if (status !== 404) {
        fail(
          `${variant.name}: ${path} answered ${status}; a variant mounted at ` +
            `${variant.basePath} must not serve the bare path`,
        );
      }
    }

    assertBundleIsClean(variant);
  }

  // An illegal value must be refused. Reading it as "unset" is the silent
  // fallback this work removed, so a normal page is the failure.
  const broken = await render(VARIANTS[0], {
    ...DEPLOYMENTS[0].env,
    AGENT_WEB_STATUS_URL: 'not-a-url',
  });
  if (broken.status < 500) {
    fail(
      `an invalid AGENT_WEB_STATUS_URL answered ${broken.status}; it must fail the ` +
        'request rather than be read as unset',
    );
  }

  console.log(
    `Runtime site config: ${VARIANTS.length} variants, ${DEPLOYMENTS.length} deployments, all correct.`,
  );
}

await main();
