// Browser path normalization, exercised through the real `env.ts` helper.
// Empty values mount at the root; configured prefixes start with a slash and
// have no trailing slash. `variantPairing.test.ts` exercises the Next config.

import { describe, expect, it } from 'vitest';

import { normalizeBasePath } from './env';

const CASES: Array<[string | undefined, string]> = [
  [undefined, ''],
  ['', ''],
  ['   ', ''],
  ['/agents', '/agents'],
  ['agents', '/agents'],
  ['/agents/', '/agents'],
  ['/agents///', '/agents'],
  ['  /agents  ', '/agents'],
  ['/a/b', '/a/b'],
  ['/', ''],
];

describe('basePath normalization', () => {
  it.each(CASES)('env.ts turns %j into %j', (raw, expected) => {
    expect(normalizeBasePath(raw)).toBe(expected);
  });
});
