import { describe, expect, it } from 'vitest';
import { workspaceLinkPath } from './workspaceLink';

describe('workspace file links', () => {
  it.each([
    ['/workspace/src/app.ts:12', 'src/app.ts'],
    ['file:///workspace/docs/readme.md#L3', 'docs/readme.md'],
    ['./src/app.ts', 'src/app.ts'],
    ['README.md', 'README.md'],
    ['Dockerfile', 'Dockerfile'],
    ['LICENSE', 'LICENSE'],
    ['/workspace/bin/tool', 'bin/tool'],
    ['docs/My%20Report.md', 'docs/My Report.md'],
  ])('resolves %s inside the sandbox', (href, path) => {
    expect(workspaceLinkPath(href)).toBe(path);
  });

  it.each([
    'https://example.org/src/app.ts',
    '//example.org/app.ts',
    'javascript:alert(1)',
    'data:text/html;base64,PHNjcmlwdD4=',
    'file:///etc/passwd',
    '/etc/passwd',
    'src/../../etc/passwd',
    'src/%2e%2e/password.txt',
    'src/%252e%252e/password.txt',
    'src\\..\\password.txt',
    'src/%00file.ts',
    '#summary',
    'dashboard',
    'README.md ',
    'src/file%broken.ts',
  ])('does not interpret %s as a workspace reference', (href) => {
    expect(workspaceLinkPath(href)).toBeNull();
  });
});
