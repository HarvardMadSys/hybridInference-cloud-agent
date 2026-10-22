import { readdirSync, readFileSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const FRONTEND = resolve(__dirname, '../..');
const TEST_LIMIT = 1200;
const ratchet = JSON.parse(readFileSync(resolve(FRONTEND, 'eslint-test-ratchet.json'), 'utf8')) as {
  overrides: Array<{
    files: string[];
    rules: { 'max-lines': ['error', { max: number }] };
  }>;
};

function filesBelow(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(root, entry.name);
    return entry.isDirectory() ? filesBelow(path) : [path];
  });
}

function testModules(): Map<string, number> {
  const sourceTests = filesBelow(resolve(FRONTEND, 'src')).filter((path) => {
    const name = relative(FRONTEND, path).replaceAll('\\', '/');
    return /\.test\.tsx?$/.test(name) || name.includes('/__tests__/');
  });
  const browserTests = filesBelow(resolve(FRONTEND, 'e2e')).filter((path) =>
    /\.(?:ts|tsx|mjs)$/.test(path),
  );
  const paths = [...sourceTests, ...browserTests];
  return new Map(
    paths.map((path) => [
      relative(FRONTEND, path).replaceAll('\\', '/'),
      readFileSync(path, 'utf8').split('\n').length - 1,
    ]),
  );
}

describe('the frontend test file-size ratchet', () => {
  it('sets a calibrated default for every test module', () => {
    const [defaultBudget] = ratchet.overrides;
    expect(defaultBudget.files).toEqual([
      '**/*.test.ts',
      '**/*.test.tsx',
      '**/__tests__/**',
      'e2e/**',
    ]);
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
