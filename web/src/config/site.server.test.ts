// The `server-only` marker, checked where it is cheap to check.
//
// The real enforcement is a build: `scripts/check-server-only-guard.mjs`
// writes a Client Component that imports the reader and requires `next build`
// to fail. That build is the proof, but it is slow and lives in CI. These
// cases are the fast net for the two things that make it work, so a change
// that quietly drops either one is caught by `npm run test` as well.
//
// This file deliberately does not import `site.server.ts`. Importing it is
// the very thing being guarded against, and the module would throw here.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const READER = join(process.cwd(), 'src', 'config', 'site.server.ts');

function readerSource(): string {
  return readFileSync(READER, 'utf8');
}

describe('the server config reader', () => {
  it("carries the `import 'server-only'` marker", () => {
    expect(readerSource()).toMatch(/^import 'server-only';$/m);
  });

  it('declares the marker first, so a later import cannot be the only guard', () => {
    // `next/server` does not stop a client bundle — a probe that imported the
    // reader from a Client Component compiled and shipped without this line.
    const imports = [...readerSource().matchAll(/^import\s.*$/gm)].map((match) => match[0]);
    expect(imports[0]).toBe("import 'server-only';");
  });

  it('resolves to a module that throws outside a server context', async () => {
    // What the bundler does for a Client Component: no `react-server`
    // condition, so `server-only` resolves to the module that throws. If this
    // ever stops throwing, the marker has stopped protecting anything.
    await expect(import('server-only')).rejects.toThrow(/Client Component/);
  });
});
