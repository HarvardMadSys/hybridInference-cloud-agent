#!/usr/bin/env node
//
// `src/config/site.server.ts` reads `process.env` and must never be reachable
// from a Client Component. Nothing in the module enforces that on its own:
// importing `next/server` does **not** stop a client bundle, and a probe that
// imported the reader from a `'use client'` module compiled successfully and
// shipped it to the browser. `import 'server-only'` is what makes the client
// bundler stop, and this check is the proof of it: it writes exactly that
// probe, builds, and requires the build to fail.
//
// A unit test cannot see this. The guard is enforced by the bundler's export
// conditions, not by anything the module does when it runs, so the only
// honest test is a real build. It is cheap — a build that fails on this error
// stops during compilation.
//
// Run from `apps/frontend/`, or from anywhere with `node apps/frontend/scripts/...`.

import { spawnSync } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const WEB_ROOT = resolve(import.meta.dirname, '..');
const NEXT_BIN = join(WEB_ROOT, 'node_modules', 'next', 'dist', 'bin', 'next');
const PROBE_DIR = join(WEB_ROOT, 'src', 'app', 'server-only-probe');
const PROBE_FILE = join(PROBE_DIR, 'page.tsx');

const PROBE = `'use client';

// Written by scripts/check-server-only-guard.mjs for the duration of one
// build, then removed. A Client Component importing the server reader is the
// mistake this check exists to make impossible.
import { readRuntimeSiteConfig } from '@/config/site.server';

export default function Probe() {
  return <p>{typeof readRuntimeSiteConfig}</p>;
}
`;

function cleanup() {
  rmSync(PROBE_DIR, { recursive: true, force: true });
}

function fail(message) {
  console.error(`::error title=Server-only guard::${message}`);
  process.exit(1);
}

// A probe left behind by an interrupted run would be picked up by the next
// build as a real route, so start from a known state.
cleanup();
mkdirSync(PROBE_DIR, { recursive: true });
writeFileSync(PROBE_FILE, PROBE);

let result;
try {
  result = spawnSync(process.execPath, [NEXT_BIN, 'build'], {
    cwd: WEB_ROOT,
    encoding: 'utf8',
    env: { ...process.env, NODE_ENV: 'production' },
  });
} finally {
  cleanup();
}

if (result.error) {
  fail(`could not run \`next build\`: ${result.error.message}`);
}

const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;

if (result.status === 0) {
  fail(
    'a Client Component imported src/config/site.server.ts and the build ' +
      "succeeded, so the module is not protected by `import 'server-only'`",
  );
}
if (!output.includes('server-only')) {
  fail(
    'the build failed, but not because of the server-only marker — something ' +
      `else is broken:\n${output.slice(-4000)}`,
  );
}
if (!output.includes('site.server.ts')) {
  fail(
    'the build failure does not name the guarded module, so it does not prove ' +
      `the guard fired:\n${output.slice(-4000)}`,
  );
}

console.log('Server-only guard: a Client Component import fails the build.');
