import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';

export default defineConfig({
  test: {
    setupFiles: ['./vitest.setup.ts'],
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
