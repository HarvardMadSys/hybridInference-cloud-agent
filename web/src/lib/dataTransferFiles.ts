// Files from a drop, including whole folders.
//
// `DataTransfer.files` flattens a dropped folder to nothing at all — the folder
// itself appears as a zero-byte entry and its contents are absent. The
// directory contents only exist behind `webkitGetAsEntry`, which is also the
// only place the relative path of each file can be recovered, so a drop that
// should behave like the folder picker has to walk the tree itself.

interface FileSystemEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  fullPath: string;
  file(onSuccess: (file: File) => void, onError: (error: unknown) => void): void;
  createReader(): {
    readEntries(
      onSuccess: (entries: FileSystemEntryLike[]) => void,
      onError: (error: unknown) => void,
    ): void;
  };
}

/** A cap on how deep a dropped tree is followed, against a pathological drop. */
const MAX_DEPTH = 16;

function readFile(entry: FileSystemEntryLike): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file(
      (file) => resolve(file),
      () => resolve(null),
    );
  });
}

/**
 * One batch of a directory's children.
 *
 * `readEntries` returns a *page*, not the whole directory, and signals the end
 * by returning an empty array. Reading it once silently truncates any folder
 * with more than about a hundred entries.
 */
function readBatch(reader: ReturnType<FileSystemEntryLike['createReader']>) {
  return new Promise<FileSystemEntryLike[]>((resolve) => {
    reader.readEntries(
      (entries) => resolve(entries),
      () => resolve([]),
    );
  });
}

async function walk(entry: FileSystemEntryLike, depth: number, into: File[]): Promise<void> {
  if (depth > MAX_DEPTH) return;
  if (entry.isFile) {
    const file = await readFile(entry);
    if (!file) return;
    // `fullPath` starts at the drop root with a leading slash; the relative
    // path is what the folder picker would have reported.
    const relative = entry.fullPath.replace(/^\/+/, '');
    Object.defineProperty(file, 'webkitRelativePath', { value: relative });
    into.push(file);
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  for (;;) {
    const batch = await readBatch(reader);
    if (batch.length === 0) return;
    for (const child of batch) await walk(child, depth + 1, into);
  }
}

/**
 * Every file a drop carries, folders expanded, relative paths preserved.
 *
 * Falls back to `DataTransfer.files` where `webkitGetAsEntry` is unavailable,
 * which loses folders but keeps plain files working rather than nothing.
 */
export async function filesFromDataTransfer(transfer: DataTransfer): Promise<File[]> {
  const items = Array.from(transfer.items ?? []);
  const entries = items
    .filter((item) => item.kind === 'file')
    .map((item) =>
      typeof item.webkitGetAsEntry === 'function'
        ? (item.webkitGetAsEntry() as FileSystemEntryLike | null)
        : null,
    )
    .filter((entry): entry is FileSystemEntryLike => entry !== null);
  if (entries.length === 0) return Array.from(transfer.files ?? []);
  const found: File[] = [];
  for (const entry of entries) await walk(entry, 0, found);
  return found;
}
