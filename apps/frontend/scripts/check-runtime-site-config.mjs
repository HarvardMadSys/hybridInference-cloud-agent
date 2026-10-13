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
// For each variant, and each of two configured deployments, this checks:
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
//      bundle at all.
//
// and then three things that are not per-deployment:
//
//   7. the RSC payload the client hydrates from carries the same identity the
//      markup does, exactly. The document proves the *server* rendered the
//      runtime values; the payload is the half a client component reads, so a
//      tree that would hydrate from the neutral default is only visible here;
//   8. `return_to` survives the mount prefix — asked to return to a path that
//      carries the prefix, the link hands the control plane a path that
//      carries it once. Applied twice it returned to `/agents/agents`;
//   9. a deployment that configures nothing renders the neutral identity, with
//      no `AGENT_WEB_*` in the environment at all. "The image carries no
//      deployment's brand" is shown rather than inferred, and a fallback that
//      named a real site is what this fails on;
//  10. an illegal value fails the request instead of being read as "unset";
//  11. the artifact itself is unchanged by any of it — a digest over the
//      standalone server and the static chunks is taken before the first
//      render and after the last, so "one image, many deployments" is checked
//      rather than inferred from the shape of the loop.
//  12. metadata and layout share one saved-settings fetch per request, while
//      the next request sees saved edits without restarting the server.
//  13. stalled settings headers or bodies cannot delay pages for seconds:
//      cold processes render neutral branding and warmed processes retain the
//      last valid appearance, with successful edits still visible on recovery.
//
// Run from `apps/frontend/`.
// Use --saved-config-only to check request-scoped caching against an existing
// production build without rebuilding either variant.

import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
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
      AGENT_DEPLOYMENT_SHA: spec.version,
      AGENT_WEB_DEPLOYED_AT: spec.deployedAt,
      // Not configuration. It is here to prove that an unrelated environment
      // value cannot ride along with the allowlisted ones.
      AGENT_SESSION_SECRET: SENTINEL,
    },
  };
}

const DEPLOYMENTS = [
  deployment({
    name: 'alpha',
    version: 'a'.repeat(40),
    deployedAt: '2026-08-07T12:00:00.000Z',
    appName: 'Alpha Agents',
    orgName: 'Alpha Research',
    orgUrl: 'https://alpha.example',
    statusUrl: 'https://status.alpha.example/',
    gatewayUrl: 'https://gateway-alpha.example/authorize',
    rag: true,
  }),
  deployment({
    name: 'beta',
    version: 'b'.repeat(40),
    deployedAt: '2026-08-08T18:00:00.000Z',
    appName: 'Beta Agents',
    orgName: 'Beta Research',
    orgUrl: 'https://beta.example',
    statusUrl: 'https://status.beta.example/',
    gatewayUrl: 'https://gateway-beta.example',
    rag: false,
  }),
];

// What a deployment that configured nothing renders.
//
// The literal is a copy of `NEUTRAL_SITE_CONFIG` in `apps/frontend/src/config/site.ts`:
// this script cannot import TypeScript, and the unit test there is what pins
// that object. Written down twice on purpose — if the default changes, this
// check fails on the next run rather than agreeing with itself.
const NEUTRAL = {
  name: 'unconfigured',
  appName: 'Cloud Agent',
  orgName: '',
  orgUrl: '',
  statusUrl: '',
  gatewayUrl: '',
  rag: false,
};

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

/**
 * The configured identity, as the pairs the assertions iterate.
 *
 * One list rather than a literal repeated per check: the document assertions,
 * the payload assertions and the foreign-deployment scan all have to agree on
 * what "the deployment's identity" is, and a field present in one copy but not
 * the others is a field that quietly stops being asserted anywhere.
 */
function identityValues(target) {
  return [
    ['app name', target.appName],
    ['organisation', target.orgName],
    ['organisation URL', target.orgUrl],
    ['status URL', target.statusUrl],
    ['gateway origin', target.gatewayOrigin],
  ];
}

/** No other deployment's identity, wherever it is being looked for. */
function assertNoForeignIdentity(where, subject, text, target) {
  for (const other of DEPLOYMENTS) {
    if (other.name === target.name) continue;
    for (const [field, value] of identityValues(other)) {
      if (text.includes(value)) {
        fail(
          `${where}: ${subject} also contains the ${other.name} ${field} ${JSON.stringify(value)}`,
        );
      }
    }
  }
}

/**
 * The RSC payload the client hydrates from, decoded.
 *
 * `self.__next_f.push([1, "..."])` is the flight data Next inlines into the
 * document, and it is what the client tree is built from. The document and the
 * payload come from one render, so this is not a second opinion about the
 * markup — it is the half a Client Component reads, and the place a tree that
 * hydrates from the neutral default instead of the configured identity shows
 * up.
 *
 * A document carrying no payload fails rather than skipping the assertions: a
 * guard whose premise has silently disappeared reads as a pass.
 */
function flightPayload(html) {
  const chunks = [...html.matchAll(/self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\]\)/g)];
  if (chunks.length === 0) {
    fail('the document carries no RSC payload, so the hydration assertions would pass vacuously');
  }
  return chunks.map((chunk) => JSON.parse(`"${chunk[1]}"`)).join('');
}

/** The identity object the client tree receives, read out of the payload. */
function serializedIdentity(payload) {
  const match = payload.match(/"branding":\{[^{}]*\}/);
  if (!match) {
    fail(
      'the RSC payload carries no serialized identity, so the client has nothing to hydrate from',
    );
  }
  return JSON.parse(match[0].slice('"branding":'.length));
}

/** The same object as it should have been serialized for this deployment. */
function expectedIdentity(target) {
  return {
    appName: target.appName,
    orgName: target.orgName,
    // Through `URL`, the way the parser keeps them: `https://alpha.example` is
    // serialized as `https://alpha.example/`, and comparing the literals would
    // report that normalization as a mismatch.
    orgUrl: new URL(target.orgUrl).toString(),
    statusUrl: new URL(target.statusUrl).toString(),
    gatewayUrl: target.gatewayOrigin,
  };
}

/** The sign-in link the document offers, as an absolute path or URL. */
function signInHref(html) {
  return firstMatch(html, /href="([^"]*\/v1\/session\/login[^"]*)"/);
}

/**
 * The same-site path the sign-in link asks the control plane to return to.
 *
 * Read back through `URL`, not matched as text: `return_to` is escaped in the
 * link, and the property under test is the value the control plane receives.
 */
function returnToOf(href) {
  return new URL(href, 'http://127.0.0.1').searchParams.get('return_to');
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

/** A digest over one directory tree, by relative path and content. */
function hashTree(root) {
  const hash = createHash('sha256');
  const walk = (dir) => {
    for (const entry of readdirSync(dir).sort()) {
      const path = join(dir, entry);
      if (entry === 'cache') continue;
      if (statSync(path).isDirectory()) walk(path);
      else {
        hash.update(path.slice(root.length));
        hash.update(readFileSync(path));
      }
    }
  };
  if (existsSync(root)) walk(root);
  return hash.digest('hex');
}

/**
 * Everything a deployment is served from: the standalone server and the static
 * chunks it references.
 *
 * The acceptance's first criterion is that the *same* artifact serves both
 * configurations. Nothing here rebuilds between renders — but that is a fact
 * about the shape of this file, and a later change that moved `next build`
 * inside the deployment loop would break the property silently. Two digests,
 * one before and one after, make it a checked property instead.
 *
 * `cache` is skipped: it is the running server's scratch space, not the
 * artifact.
 */
function artifactDigest() {
  return `${hashTree(STANDALONE)}:${hashTree(STATIC_DIR)}`;
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
  // The deployment's identity is given, never inherited: a shell or CI job that
  // happens to export one of these must not decide what any render below shows.
  const inherited = Object.fromEntries(
    Object.entries(process.env).filter(([key]) => !key.startsWith('AGENT_WEB_')),
  );
  const child = spawn(process.execPath, [SERVER], {
    cwd: STANDALONE,
    env: {
      ...inherited,
      NODE_ENV: 'production',
      HOSTNAME: '127.0.0.1',
      PORT: String(port),
      ...env,
    },
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

async function render(variant, env, suffix = '/login') {
  const port = nextPort++;
  const server = startServer(env, port);
  try {
    const url = `http://127.0.0.1:${port}${variant.basePath}${suffix}`;
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

  const footer = textOf(html, 'footer');
  if (target.version) {
    if (
      !footer.includes(target.version.slice(0, 7)) ||
      !footer.includes(`${target.deployedAt.slice(0, 10)} UTC`) ||
      !html.includes(`dateTime="${target.deployedAt}"`)
    ) {
      fail(`${where}: the footer does not show this deployment's version and date`);
    }
  } else if (!footer.includes('Not available')) {
    fail(`${where}: an unconfigured footer must not invent deployment metadata`);
  }

  // The one URL this app hands to another service, and the string the gateway
  // matches as an exact redirect target.
  const signIn = signInHref(html);
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
  for (const [field, value] of identityValues(target)) {
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

  // The half the client hydrates from. Compared as an object rather than
  // searched for as substrings: a payload carrying one field of this
  // deployment and the rest of another's is also a document that renders
  // correctly and hydrates into someone else's site.
  const payload = flightPayload(html);
  const identity = serializedIdentity(payload);
  if (JSON.stringify(identity) !== JSON.stringify(expectedIdentity(target))) {
    fail(
      `${where}: the RSC payload serializes ${JSON.stringify(identity)}, expected ` +
        `${JSON.stringify(expectedIdentity(target))}`,
    );
  }

  const rag = payload.match(/"features":\{"rag":(true|false)\}/);
  if (!rag) {
    fail(`${where}: the RSC payload carries no serialized feature switch`);
  }
  if (rag[1] !== String(target.rag)) {
    fail(`${where}: the RSC payload's feature switch is ${rag[1]}, expected ${target.rag}`);
  }

  // Anything belonging to the other deployment means a fallback to a
  // build-time constant, which is the whole failure this work removed.
  assertNoForeignIdentity(where, 'the document', html, target);
  assertNoForeignIdentity(where, 'the RSC payload', payload, target);

  if (html.includes(SENTINEL)) {
    fail(`${where}: a variable that is not on the allowlist reached the HTML`);
  }
  if (payload.includes(SENTINEL)) {
    fail(`${where}: a variable that is not on the allowlist reached the RSC payload`);
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

/**
 * The mount prefix reaches the control plane once.
 *
 * `return_to` is captured from `window.location.pathname`, which already
 * carries the prefix, and the control plane appends it back when it builds the
 * landing URL. Handed over unchanged it is applied twice: signing in at
 * `/agents` returned to `/agents/agents`, which this app resolved as a job
 * whose id was `agents` and rendered as "No such agent job". The unit tests pin
 * `safeReturnTo`; this pins what the built page actually links to.
 */
async function assertReturnTo(variant) {
  const landing = '/jobs/abc';
  const asked = `${variant.basePath}${landing}`;
  const where = `${variant.name} / return_to`;
  const rendered = await render(
    variant,
    DEPLOYMENTS[0].env,
    `/login?return_to=${encodeURIComponent(asked)}`,
  );
  if (rendered.status !== 200) {
    fail(`${where}: the page answered ${rendered.status}\n${rendered.logs}`);
  }

  const href = signInHref(rendered.html);
  if (!href) fail(`${where}: no sign-in link in the document`);

  const returnTo = returnToOf(href);
  if (returnTo !== landing) {
    fail(
      `${where}: asked to return to ${JSON.stringify(asked)}, the sign-in link asks the control ` +
        `plane for ${JSON.stringify(returnTo)}, expected ${JSON.stringify(landing)}`,
    );
  }
}

/**
 * A deployment that configured nothing.
 *
 * The design asks for an image that is built and run without any of this
 * configuration, still starts, and shows a generic site — with no real
 * deployment's name as what it falls back to. The unit tests cover the parser;
 * this covers the built artifact, which is where a module-level default would
 * live.
 */
async function assertNeutralDeployment(variant) {
  const where = `${variant.name} / ${NEUTRAL.name}`;
  const rendered = await render(variant, { AGENT_SESSION_SECRET: SENTINEL });
  if (rendered.status !== 200) {
    fail(`${where}: GET ${variant.basePath}/login answered ${rendered.status}\n${rendered.logs}`);
  }

  const title = titleOf(rendered.html);
  if (title !== NEUTRAL.appName) {
    fail(
      `${where}: <title> is ${JSON.stringify(title)}, expected the neutral ` +
        `${JSON.stringify(NEUTRAL.appName)}`,
    );
  }

  const heading = textOf(rendered.html, 'h1');
  if (heading !== NEUTRAL.appName) {
    fail(
      `${where}: the rendered <h1> is ${JSON.stringify(heading)}, expected the neutral ` +
        `${JSON.stringify(NEUTRAL.appName)}`,
    );
  }

  const payload = flightPayload(rendered.html);
  const identity = serializedIdentity(payload);
  const expected = {
    appName: NEUTRAL.appName,
    orgName: NEUTRAL.orgName,
    orgUrl: NEUTRAL.orgUrl,
    statusUrl: NEUTRAL.statusUrl,
    gatewayUrl: NEUTRAL.gatewayUrl,
  };
  if (JSON.stringify(identity) !== JSON.stringify(expected)) {
    fail(
      `${where}: the RSC payload serializes ${JSON.stringify(identity)}, expected the neutral ` +
        `identity ${JSON.stringify(expected)}`,
    );
  }

  const rag = payload.match(/"features":\{"rag":(true|false)\}/);
  if (!rag || rag[1] !== String(NEUTRAL.rag)) {
    fail(`${where}: the feature switch is ${rag ? rag[1] : 'absent'}, expected ${NEUTRAL.rag}`);
  }

  assertNoForeignIdentity(where, 'the document', rendered.html, NEUTRAL);
  assertNoForeignIdentity(where, 'the RSC payload', payload, NEUTRAL);

  if (rendered.html.includes(SENTINEL) || payload.includes(SENTINEL)) {
    fail(`${where}: a variable that is not on the allowlist reached an unconfigured deployment`);
  }
}

/** One settings snapshot per render, refreshed on the next request. */
async function assertSavedConfigRequestScope(variant) {
  let target = DEPLOYMENTS[0];
  let reads = 0;
  let mode = 'slow headers';
  const fallbackTimings = [];
  const controlPlane = createServer((request, response) => {
    if (request.url !== '/v1/agent/site-config') {
      response.writeHead(404).end();
      return;
    }
    reads += 1;
    const saved = {
      branding: expectedIdentity(target),
      features: { rag: target.rag },
    };
    if (mode === 'http') {
      response.writeHead(503).end();
      return;
    }
    if (mode === 'invalid') {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ ...saved, features: { rag: 'invalid' } }));
      return;
    }
    if (mode === 'slow body') {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.write(JSON.stringify(saved).slice(0, 1));
    }
    // Keep the two readers overlapping: a module cache or Next's fetch
    // deduplication behaving differently must still satisfy the request count.
    const slowBody = mode === 'slow body';
    const timer = setTimeout(
      () => {
        if (slowBody) response.end(JSON.stringify(saved).slice(1));
        else {
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end(JSON.stringify(saved));
        }
      },
      mode.startsWith('slow') ? 5_000 : 20,
    );
    response.on('close', () => clearTimeout(timer));
  });
  await new Promise((done) => controlPlane.listen(0, '127.0.0.1', done));

  const port = nextPort++;
  const server = startServer(
    {
      ...DEPLOYMENTS[0].env,
      AGENT_CONTROL_PLANE_URL: `http://127.0.0.1:${controlPlane.address().port}`,
    },
    port,
  );
  const url = `http://127.0.0.1:${port}${variant.basePath}/login`;

  async function assertSnapshot(expected, label, bounded = false) {
    const before = reads;
    const started = performance.now();
    const response = await fetch(url, { signal: AbortSignal.timeout(2_500) });
    const html = await response.text();
    const elapsed = performance.now() - started;
    const where = `${variant.name} / ${label}`;
    if (response.status !== 200) {
      throw new Error(`${where}: page answered ${response.status}\n${server.logs.join('')}`);
    }
    if (reads - before !== 1) {
      throw new Error(`${where}: rendered one page with ${reads - before} settings fetches`);
    }
    if (titleOf(html) !== expected.appName || textOf(html, 'h1') !== expected.appName) {
      throw new Error(`${where}: metadata and layout did not read the expected saved name`);
    }
    const identity = serializedIdentity(flightPayload(html));
    if (JSON.stringify(identity) !== JSON.stringify(expected)) {
      throw new Error(`${where}: hydration did not receive the expected saved identity`);
    }
    // This includes the complete HTML, not merely streamed response headers.
    // Allow rendering/CI overhead beyond the unit-tested 400ms read budget,
    // while rejecting the previous five-second configuration bottleneck.
    if (bounded) {
      fallbackTimings.push(elapsed);
      if (elapsed > 1_500) {
        throw new Error(`${where}: fallback page took ${elapsed.toFixed(0)}ms (limit 1500ms)`);
      }
    }
  }

  try {
    // Start the production process through an asset request, so its startup
    // time cannot hide the appearance budget or prime the public fallback.
    const chunk = readdirSync(join(STATIC_DIR, 'chunks')).find((file) =>
      /^main-app-.*\.js$/.test(file),
    );
    if (!chunk) throw new Error('No main-app chunk to check production server readiness');
    const asset = await waitForResponse(
      `http://127.0.0.1:${port}${variant.basePath}/_next/static/chunks/${chunk}`,
    );
    await asset.arrayBuffer();
    if (asset.status !== 200 || reads !== 0) {
      throw new Error(`${variant.name}: static readiness request unexpectedly read settings`);
    }

    const neutral = {
      appName: NEUTRAL.appName,
      orgName: '',
      orgUrl: '',
      statusUrl: '',
      gatewayUrl: '',
    };
    for (const stalled of ['slow headers', 'slow body']) {
      mode = stalled;
      await assertSnapshot(neutral, `cold ${mode}`, true);
    }

    mode = 'success';
    for (const deployment of DEPLOYMENTS) {
      target = deployment;
      await assertSnapshot(expectedIdentity(target), `saved ${target.name}`);
    }

    for (const failure of ['slow headers', 'slow body', 'http', 'invalid']) {
      mode = failure;
      await assertSnapshot(expectedIdentity(target), `retained appearance after ${mode}`, true);
    }

    mode = 'success';
    target = DEPLOYMENTS[0];
    await assertSnapshot(expectedIdentity(target), 'saved edits after backend recovery');

    // Separate concurrent page requests must each read their own settings,
    // rather than sharing a process-wide in-flight promise.
    const before = reads;
    const responses = await Promise.all([fetch(url), fetch(url)]);
    await Promise.all(responses.map((response) => response.text()));
    if (responses.some((response) => response.status !== 200) || reads - before !== 2) {
      throw new Error(`${variant.name}: concurrent pages did not each read one settings snapshot`);
    }
    console.log(
      `${variant.name}: cold and retained appearance fallbacks rendered within ` +
        `${Math.max(...fallbackTimings).toFixed(0)}ms.`,
    );
  } finally {
    await server.stop();
    await new Promise((done, reject) =>
      controlPlane.close((error) => (error ? reject(error) : done())),
    );
  }
}

async function main() {
  for (const variant of VARIANTS) {
    buildVariant(variant);
    const built = artifactDigest();

    for (const target of DEPLOYMENTS) {
      assertRendered(variant, target, await render(variant, target.env));
    }

    await assertReturnTo(variant);
    await assertNeutralDeployment(variant);
    await assertSavedConfigRequestScope(variant);

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

    // The artifact served every deployment above, unchanged. A digest that
    // moved means something rebuilt or rewrote it, and "the same image serves
    // both configurations" would no longer be what was checked.
    const served = artifactDigest();
    if (served !== built) {
      fail(
        `${variant.name}: the artifact changed while it served the deployments ` +
          `(${built} -> ${served}) — the digest is not fixed`,
      );
    }
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
    `Runtime site config: ${VARIANTS.length} variants, ${DEPLOYMENTS.length} configured ` +
      `deployments, 1 unconfigured deployment, all correct.`,
  );
}

if (process.argv.includes('--saved-config-only')) {
  const { basePath } = JSON.parse(readFileSync(join(NEXT_DIR, 'routes-manifest.json'), 'utf8'));
  const variant = VARIANTS.find((item) => item.basePath === basePath);
  if (!variant)
    throw new Error(`No runtime config fixture for basePath ${JSON.stringify(basePath)}`);
  await assertSavedConfigRequestScope(variant);
  console.log(
    'Runtime site config: one fetch per request, fresh edits, and concurrent pages correct.',
  );
} else {
  await main();
}
