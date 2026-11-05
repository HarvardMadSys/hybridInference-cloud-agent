import { availableParallelism } from 'node:os';
import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

import { VITEST_TEST_EXCLUDE, VITEST_TEST_INCLUDE } from './test-discovery.mjs';

/**
 * At most this many workers, however many CPUs the machine has.
 *
 * A hosted CI VM has four, and the suite uses all of them; Vitest otherwise
 * reserves one, slowing the jsdom-heavy suite there. The trusted runners share
 * one 20-core host with every other job of a push, and taking all 20 there
 * starved the jobs beside it and this suite's own timed tests. Eight takes the
 * suite from 27 s to about 35 s on that host when it is idle.
 */
const MAX_WORKERS = 8;

export default defineConfig({
  test: {
    include: VITEST_TEST_INCLUDE,
    exclude: VITEST_TEST_EXCLUDE,
    setupFiles: ['./vitest.setup.ts'],
    maxWorkers: Math.min(availableParallelism(), MAX_WORKERS),
    // The default 5 s is a deadline for one test's own work. On a shared CI
    // host the same test waits for a CPU, and the default failed unchanged
    // code there (`JobDetail.performance` among others). A test that hangs
    // still fails, later.
    testTimeout: 20_000,
    // Node 25+ exposes its own experimental Web Storage globals. Vitest sees
    // those names before jsdom is installed and therefore skips jsdom's
    // localStorage/sessionStorage implementations, leaving localStorage
    // undefined unless Node was given a persistence file. Older supported
    // Node releases do not recognize this flag, so pass it only to newer
    // workers.
    execArgv:
      Number.parseInt(process.versions.node.split('.')[0] ?? '0', 10) >= 25
        ? ['--no-experimental-webstorage']
        : [],
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  oxc: {
    jsx: { runtime: 'automatic' },
  },
});
