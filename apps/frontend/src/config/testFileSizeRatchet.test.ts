import { readdirSync, readFileSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  CODE_MODULE_PATTERN,
  ESLINT_TEST_GLOBS,
  TEST_MODULE_PATTERN,
  VITEST_TEST_DIRECTORIES_TO_SKIP,
  VITEST_TEST_EXCLUDE,
  VITEST_TEST_INCLUDE,
} from '../../test-discovery.mjs';

const FRONTEND = resolve(__dirname, '../..');
const TEST_LIMIT = 1200;
const EXCLUDED_DIRECTORIES = new Set<string>(VITEST_TEST_DIRECTORIES_TO_SKIP);
const ratchet = JSON.parse(readFileSync(resolve(FRONTEND, 'eslint-test-ratchet.json'), 'utf8')) as {
  overrides: Array<{
    files: string[];
    rules: { 'max-lines': ['error', { max: number }] };
  }>;
};

function filesBelow(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    if (entry.isDirectory() && EXCLUDED_DIRECTORIES.has(entry.name)) {
      return [];
    }
    const path = resolve(root, entry.name);
    return entry.isDirectory() ? filesBelow(path) : [path];
  });
}

function isRatchetedTestModule(name: string): boolean {
  if (name.split('/').some((part) => EXCLUDED_DIRECTORIES.has(part))) {
    return false;
  }
  return (
    TEST_MODULE_PATTERN.test(name) || (name.startsWith('e2e/') && CODE_MODULE_PATTERN.test(name))
  );
}

function lineCount(path: string): number {
  const source = readFileSync(path, 'utf8');
  return source === '' ? 0 : source.split('\n').length - (source.endsWith('\n') ? 1 : 0);
}

function testModules(): Map<string, number> {
  return new Map(
    filesBelow(FRONTEND).flatMap((path) => {
      const name = relative(FRONTEND, path).replaceAll('\\', '/');
      return isRatchetedTestModule(name) ? [[name, lineCount(path)] as const] : [];
    }),
  );
}

describe('the frontend test file-size ratchet', () => {
  it('covers every supported Vitest location and extension but not generated output', () => {
    expect(VITEST_TEST_INCLUDE).toEqual(ESLINT_TEST_GLOBS.slice(0, 1));
    expect(VITEST_TEST_EXCLUDE).toEqual([
      '**/node_modules/**',
      '**/.git/**',
      '**/.next/**',
      '**/build/**',
      '**/dist/**',
      '**/coverage/**',
      '**/playwright-report/**',
      '**/test-results/**',
      'e2e/**',
    ]);

    const included = [
      'root.test.js',
      'root.spec.jsx',
      'scripts/probe.test.ts',
      'src/probe.spec.tsx',
      'src/probe.test.mjs',
      'src/probe.test.mjsx',
      'src/probe.spec.cjs',
      'src/probe.spec.cjsx',
      'src/probe.test.mts',
      'src/probe.test.mtsx',
      'src/probe.spec.cts',
      'src/probe.spec.ctsx',
      'e2e/helper.ts',
      'e2e/recovery.spec.ts',
    ];
    const excluded = [
      'src/not-a-test.ts',
      'src/probe.test.json',
      'node_modules/pkg/probe.test.ts',
      '.git/probe.spec.ts',
      '.next/server/probe.spec.js',
      'build/probe.test.ts',
      'dist/probe.test.ts',
      'coverage/probe.test.ts',
      'playwright-report/probe.test.ts',
      'test-results/probe.test.ts',
    ];

    expect(included.filter((path) => !isRatchetedTestModule(path))).toEqual([]);
    expect(excluded.filter(isRatchetedTestModule)).toEqual([]);
  });

  it('sets a calibrated default for every test module', () => {
    const [defaultBudget] = ratchet.overrides;
    expect(defaultBudget.files).toEqual(ESLINT_TEST_GLOBS);
    expect(defaultBudget.rules['max-lines']).toEqual([
      'error',
      { max: TEST_LIMIT, skipBlankLines: false, skipComments: false },
    ]);
  });

  it('has an exact, shrink-only entry for every current offender', () => {
    const actual = new Map(
      [...testModules()]
        .filter(([, lines]) => lines > TEST_LIMIT)
        .sort(([a], [b]) => a.localeCompare(b)),
    );
    const baseline = new Map(
      ratchet.overrides
        .slice(1)
        .flatMap((override) =>
          override.files.map((path) => [path, override.rules['max-lines'][1].max] as const),
        )
        .sort(([a], [b]) => a.localeCompare(b)),
    );
    expect(baseline).toEqual(actual);
  });
});
