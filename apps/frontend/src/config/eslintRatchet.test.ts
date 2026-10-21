// `eslint-ratchet.json` raises the complexity and size limits for files that
// were already over them (#771). ESLint ignores an entry that matches no file,
// so an entry left behind by a deleted or renamed file would quietly lend its
// higher limit to the next file created at that path.

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const FRONTEND = resolve(__dirname, '../..');

const ratchet = JSON.parse(readFileSync(resolve(FRONTEND, 'eslint-ratchet.json'), 'utf8')) as {
  overrides: Array<{ files: string[] }>;
};

describe('eslint-ratchet.json', () => {
  it('names only files that exist', () => {
    const named = ratchet.overrides
      .flatMap((override) => override.files)
      .filter((pattern) => !pattern.includes('*'))
      // A dynamic route segment is escaped in the glob: `\[threadId\]`.
      .map((pattern) => pattern.replace(/\\([[\]])/g, '$1'));

    expect(named.length).toBeGreaterThan(0);
    expect(named.filter((path) => !existsSync(resolve(FRONTEND, path)))).toEqual([]);
  });
});
