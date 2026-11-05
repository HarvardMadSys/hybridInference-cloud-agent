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
const extensionPattern = TEST_EXTENSIONS.join('|');

export const VITEST_TEST_INCLUDE = [`**/*.{test,spec}.{${extensionGlob}}`];
export const VITEST_TEST_DIRECTORIES_TO_SKIP = [
  'node_modules',
  '.git',
  '.next',
  'build',
  'dist',
  'coverage',
  'playwright-report',
  'test-results',
];
export const VITEST_TEST_EXCLUDE = [
  ...VITEST_TEST_DIRECTORIES_TO_SKIP.map((directory) => `**/${directory}/**`),
  'e2e/**',
];
export const ESLINT_TEST_GLOBS = [...VITEST_TEST_INCLUDE, `e2e/**/*.{${extensionGlob}}`];
export const TEST_MODULE_PATTERN = new RegExp(`\\.(?:test|spec)\\.(?:${extensionPattern})$`);
export const CODE_MODULE_PATTERN = new RegExp(`\\.(?:${extensionPattern})$`);
