import { describe, expect, it } from 'vitest';

import { isSafeUploadPath, uploadPathFor } from './attachmentPath';
import { DEFAULT_ATTACHMENT_LIMITS, formatBytes } from './attachmentLimits';

const MAX = DEFAULT_ATTACHMENT_LIMITS.max_path_length;

describe('isSafeUploadPath', () => {
  it.each(['a.txt', 'specs/api.md', 'a/b/c/d.png', '空格 和中文.md', 'dot.in.name.tar.gz'])(
    'accepts the ordinary relative path %s',
    (path) => {
      expect(isSafeUploadPath(path, MAX)).toBe(true);
    },
  );

  it.each([
    '../escape.txt',
    'a/../../escape.txt',
    '/absolute.txt',
    'C:/windows.txt',
    'C:\\windows.txt',
    '.git/config',
    'a/.GIT/config',
    'a//b.txt',
    './relative.txt',
    'back\\slash.txt',
    '',
    'a\nb.txt',
    'a\rb.txt',
    'tab\tname.txt',
    '   ',
  ])('refuses %s', (path) => {
    expect(isSafeUploadPath(path, MAX)).toBe(false);
  });

  it.each([' report.csv ', 'notes.txt ', ' specs/api.md '])(
    'accepts %s with its surrounding whitespace intact',
    (path) => {
      expect(isSafeUploadPath(path, MAX)).toBe(true);
    },
  );

  it('refuses a path longer than the server will accept', () => {
    expect(isSafeUploadPath('a'.repeat(MAX), MAX)).toBe(true);
    expect(isSafeUploadPath('a'.repeat(MAX + 1), MAX)).toBe(false);
  });
});

describe('uploadPathFor', () => {
  function file(name: string, relative?: string): File {
    const made = new File(['x'], name);
    if (relative !== undefined) {
      Object.defineProperty(made, 'webkitRelativePath', { value: relative });
    }
    return made;
  }

  it('uses the bare name when a single file was picked', () => {
    expect(uploadPathFor(file('notes.md'))).toBe('notes.md');
  });

  it('keeps a folder upload’s own structure', () => {
    expect(uploadPathFor(file('api.md', 'specs/api.md'))).toBe('specs/api.md');
  });

  it('treats an empty relative path as no relative path', () => {
    expect(uploadPathFor(file('notes.md', ''))).toBe('notes.md');
  });

  it('drops the leading ./ some browsers emit, and nothing else', () => {
    expect(uploadPathFor(file('api.md', './specs/api.md'))).toBe('specs/api.md');
    // An unsafe path is reported, never repaired: this one stays unsafe so the
    // caller refuses it rather than silently uploading something else.
    expect(uploadPathFor(file('api.md', '../specs/api.md'))).toBe('../specs/api.md');
  });
});

describe('formatBytes', () => {
  it('reads the way a file size is written', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(1887436)).toBe('1.8 MB');
    // The limits are documented in whole megabytes; the message quoting one
    // should not read "5.0 MB".
    expect(formatBytes(DEFAULT_ATTACHMENT_LIMITS.max_file_bytes)).toBe('5 MB');
    expect(formatBytes(DEFAULT_ATTACHMENT_LIMITS.max_total_bytes)).toBe('20 MB');
  });
});
