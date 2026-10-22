import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ESLINT_TEST_GLOBS,
  VITEST_TEST_DIRECTORIES_TO_SKIP,
  VITEST_TEST_EXCLUDE,
  VITEST_TEST_INCLUDE,
} from './test-discovery.mjs';

const TEST_EXTENSIONS = [
  'js',
  'jsx',
  'ts',
  'tsx',
  'cjs',
  'cjsx',
  'cts',
  'ctsx',
  'mjs',
  'mjsx',
  'mts',
  'mtsx',
];
const extensionGlob = TEST_EXTENSIONS.join(',');
const REQUIRED_VITEST_INCLUDE = [`**/*.{test,spec}.{${extensionGlob}}`];
const REQUIRED_DIRECTORIES_TO_SKIP = [
  'node_modules',
  '.git',
  '.next',
  'build',
  'dist',
  'coverage',
  'playwright-report',
  'test-results',
];
const REQUIRED_VITEST_EXCLUDE = [
  ...REQUIRED_DIRECTORIES_TO_SKIP.map((directory) => `**/${directory}/**`),
  'e2e/**',
];
const REQUIRED_ESLINT_TEST_GLOBS = [...REQUIRED_VITEST_INCLUDE, `e2e/**/*.{${extensionGlob}}`];

function assertCompleteDiscovery({
  vitestInclude,
  directoriesToSkip,
  vitestExclude,
  eslintTestGlobs,
}) {
  assert.deepStrictEqual(
    vitestInclude,
    REQUIRED_VITEST_INCLUDE,
    'VITEST_TEST_INCLUDE must cover test/spec files in all 12 JavaScript and TypeScript forms',
  );
  assert.deepStrictEqual(
    directoriesToSkip,
    REQUIRED_DIRECTORIES_TO_SKIP,
    'Vitest directory exclusions must remain complete',
  );
  assert.deepStrictEqual(
    vitestExclude,
    REQUIRED_VITEST_EXCLUDE,
    'Vitest exclusions must derive from the complete directory list and keep e2e separate',
  );
  assert.deepStrictEqual(
    eslintTestGlobs,
    REQUIRED_ESLINT_TEST_GLOBS,
    'ESLint must cover the Vitest forms plus browser tests',
  );
}

const currentDiscovery = {
  vitestInclude: VITEST_TEST_INCLUDE,
  directoriesToSkip: VITEST_TEST_DIRECTORIES_TO_SKIP,
  vitestExclude: VITEST_TEST_EXCLUDE,
  eslintTestGlobs: ESLINT_TEST_GLOBS,
};

test('the standalone guard pins complete frontend test discovery', () => {
  assertCompleteDiscovery(currentDiscovery);
});

test('narrowing Vitest discovery fails outside the suite it would hide', () => {
  assert.throws(
    () => assertCompleteDiscovery({ ...currentDiscovery, vitestInclude: ['**/*.test.tsx'] }),
    /VITEST_TEST_INCLUDE must cover/,
  );
});
