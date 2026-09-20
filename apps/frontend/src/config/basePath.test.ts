// `basePath` is normalized in two places and they have to agree.
//
// `next.config.js` is plain CommonJS, loaded before any TypeScript exists, so
// it cannot import `env.ts`. The logic is therefore duplicated — and a
// duplicate that drifts is worse than either version alone: Next would mount
// the app at one prefix while the OAuth redirect named another, and the only
// symptom would be the gateway refusing an exact-match redirect URI.

import { describe, expect, it } from 'vitest';

import { normalizeBasePath } from './env';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const nextConfigNormalize = (raw: string | undefined): string => {
  // The exact expression from next.config.js. Copied rather than imported:
  // requiring that file executes it against the ambient environment, and it
  // reads `process.env` at module scope.
  const trimmed = (raw || '').trim().replace(/\/+$/, '');
  return !trimmed ? '' : trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
};

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

  it.each(CASES)('next.config.js agrees on %j', (raw, expected) => {
    expect(nextConfigNormalize(raw)).toBe(expected);
  });

  it('never produces a trailing slash, which would double every derived path', () => {
    for (const [raw] of CASES) {
      expect(normalizeBasePath(raw)).not.toMatch(/\/$/);
    }
  });

  it('never produces a relative prefix, which would resolve against the current page', () => {
    for (const [raw] of CASES) {
      const value = normalizeBasePath(raw);
      if (value) expect(value.startsWith('/')).toBe(true);
    }
  });
});
