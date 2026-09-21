// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { filesFromDataTransfer } from './dataTransferFiles';

/** A `FileSystemEntry` for one file, as the browser hands it to a drop. */
function fileEntry(fullPath: string, body = 'x') {
  const name = fullPath.split('/').pop()!;
  return {
    isFile: true,
    isDirectory: false,
    fullPath,
    file: (ok: (file: File) => void) => ok(new File([body], name)),
    createReader: () => ({ readEntries: (ok: (e: never[]) => void) => ok([]) }),
  };
}

/** A directory entry whose reader pages, the way real ones do. */
function directoryEntry(fullPath: string, pages: unknown[][]) {
  let at = 0;
  return {
    isFile: false,
    isDirectory: true,
    fullPath,
    file: () => {},
    createReader: () => ({
      readEntries: (ok: (entries: unknown[]) => void) => ok(at < pages.length ? pages[at++]! : []),
    }),
  };
}

function transfer(entries: unknown[], files: File[] = []): DataTransfer {
  return {
    items: entries.map((entry) => ({ kind: 'file', webkitGetAsEntry: () => entry })),
    files,
  } as unknown as DataTransfer;
}

describe('filesFromDataTransfer', () => {
  it('reads a plain dropped file', async () => {
    const found = await filesFromDataTransfer(transfer([fileEntry('/notes.md')]));
    expect(found.map((file) => file.name)).toEqual(['notes.md']);
  });

  it('gives each file the relative path the folder picker would have', async () => {
    const found = await filesFromDataTransfer(
      transfer([
        directoryEntry('/specs', [
          [
            fileEntry('/specs/api.md'),
            directoryEntry('/specs/diagrams', [[fileEntry('/specs/diagrams/flow.svg')]]),
          ],
        ]),
      ]),
    );
    expect(
      found.map((file) => (file as File & { webkitRelativePath: string }).webkitRelativePath),
    ).toEqual(['specs/api.md', 'specs/diagrams/flow.svg']);
  });

  it('keeps reading a directory until its reader is empty', async () => {
    // readEntries returns a page at a time; stopping at the first would
    // silently drop everything past roughly a hundred files.
    const found = await filesFromDataTransfer(
      transfer([
        directoryEntry('/big', [
          [fileEntry('/big/1.txt')],
          [fileEntry('/big/2.txt')],
          [fileEntry('/big/3.txt')],
        ]),
      ]),
    );
    expect(found.map((file) => file.name)).toEqual(['1.txt', '2.txt', '3.txt']);
  });

  it('stops following a pathologically deep tree', async () => {
    let entry: ReturnType<typeof directoryEntry> = directoryEntry('/d20', [
      [fileEntry('/d20/deep.txt')],
    ]);
    for (let depth = 19; depth >= 0; depth -= 1) {
      entry = directoryEntry(`/d${depth}`, [[entry]]);
    }
    expect(await filesFromDataTransfer(transfer([entry]))).toEqual([]);
  });

  it('falls back to the flat file list where entries are unavailable', async () => {
    const plain = new File(['x'], 'legacy.txt');
    const dropped = {
      items: [{ kind: 'file' }],
      files: [plain],
    } as unknown as DataTransfer;
    expect(await filesFromDataTransfer(dropped)).toEqual([plain]);
  });
});
