// A workspace patch read as the files it changes and the lines it adds and removes.

import type { AgentDiffFile, DiffLine } from '../types';

// `diff --git <old> <new>`. Git quotes either name on its own when it holds a
// double quote, a backslash, a control character or, unless core.quotePath is
// off, a byte outside ASCII.
const DIFF_HEADER = /^diff --git (?:a\/.+?|"(?:[^"\\]|\\.)*") (?:b\/(.+)|"b\/((?:[^"\\]|\\.)*)")$/;

// The single-letter escapes Git's quote_c_style writes. Every other byte it
// escapes is three octal digits, so a UTF-8 name arrives one byte at a time.
const GIT_PATH_ESCAPES: Readonly<Record<string, number>> = {
  a: 0x07,
  b: 0x08,
  t: 0x09,
  n: 0x0a,
  v: 0x0b,
  f: 0x0c,
  r: 0x0d,
  '"': 0x22,
  '\\': 0x5c,
};

/** The name a quoted Git path spells, or null when its bytes are not UTF-8. */
function unquoteGitPath(quoted: string): string | null {
  const encoder = new TextEncoder();
  const bytes: number[] = [];
  for (const [, escape, text] of quoted.matchAll(/\\([0-3][0-7]{2}|.)|([^\\]+)/g)) {
    if (text !== undefined) bytes.push(...encoder.encode(text));
    else if (escape.length === 3) bytes.push(Number.parseInt(escape, 8));
    else bytes.push(GIT_PATH_ESCAPES[escape] ?? escape.charCodeAt(0));
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes));
  } catch {
    // Read loosely, such a name would show a path with U+FFFD in it that no
    // file has, and two names could read the same. The workspace leaves it
    // out of its listing too.
    return null;
  }
}

/**
 * The path a `diff --git` header gives the changed file, or null for any other
 * line and for a header naming no file the workspace can open: one whose name
 * is not UTF-8, or whose prefixes a repository's diff config changed.
 */
function diffHeaderPath(line: string): string | null {
  const header = DIFF_HEADER.exec(line);
  if (!header) return null;
  const path = header[1] ?? unquoteGitPath(header[2]);
  return path || null;
}

/** Extract the file list a patch touches, for the diff header. */
export function toDiffFiles(patch: string): string[] {
  const files = new Set<string>();
  for (const line of patch.split('\n')) {
    const path = diffHeaderPath(line);
    if (path !== null) files.add(path);
  }
  return [...files];
}

/** Split a patch into independently selectable files with per-file stats. */
export function toDiffFileDetails(patch: string): AgentDiffFile[] {
  if (!patch) return [];
  const files: AgentDiffFile[] = [];
  let current: AgentDiffFile | null = null;
  // `---` and `+++` name the file only before its first hunk. Inside a hunk the
  // first character says what a line is, so a removed `--count;` is a removal.
  let inHunk = false;

  for (const line of patch.split('\n')) {
    if (line.startsWith('diff --git ')) {
      // Every header starts a new file but one naming the file just read: Git
      // writes a change of file type, such as a file that became a symlink,
      // as a deletion and a creation under one name. A file the workspace
      // cannot name is left out with its lines, rather than adding them to
      // the file before it.
      const path = diffHeaderPath(line);
      inHunk = false;
      if (current !== null && path === current.path) continue;
      current = path === null ? null : { path, add: 0, del: 0, lines: [] };
      if (current) files.push(current);
      continue;
    }
    if (!current || line.startsWith('index ')) continue;

    let marker: DiffLine['marker'] = 'ctx';
    if (line.startsWith('@@')) {
      marker = 'hunk';
      inHunk = true;
    } else if (inHunk && line.startsWith('+')) {
      marker = 'add';
      current.add += 1;
    } else if (inHunk && line.startsWith('-')) {
      marker = 'del';
      current.del += 1;
    }
    current.lines.push({ marker, text: line });
  }
  return files;
}

/**
 * The lines a patch adds and removes, counted from its hunks.
 *
 * Not from the files its headers name: a repository's diff.noPrefix or
 * diff.mnemonicPrefix writes headers `toDiffFileDetails` does not read, and the
 * count must not drop to nothing for them. A line is read as there: by its first
 * character inside a hunk, with `---` and `+++` headers only before one.
 */
function diffStat(patch: string): { add: number; del: number } | undefined {
  if (!patch) return undefined;
  let add = 0;
  let del = 0;
  let inHunk = false;
  for (const line of patch.split('\n')) {
    if (line.startsWith('diff --git ')) inHunk = false;
    else if (line.startsWith('@@')) inHunk = true;
    else if (inHunk && line.startsWith('+')) add += 1;
    else if (inHunk && line.startsWith('-')) del += 1;
  }
  return { add, del };
}

/**
 * The workspace patch, read once per distinct patch.
 *
 * A running turn's stream projects the conversation again for each event,
 * while its patch only changes when the workspace is read again. Reading it
 * three ways on every event allocated one object per patch line each time.
 * The results are never mutated, so sharing them is safe.
 */
let lastPatch:
  | {
      patch: string;
      files: string[];
      details: AgentDiffFile[];
      stat: ReturnType<typeof diffStat>;
    }
  | undefined;

export function parsePatch(patch: string): NonNullable<typeof lastPatch> {
  if (lastPatch?.patch !== patch) {
    const details = toDiffFileDetails(patch);
    lastPatch = {
      patch,
      files: toDiffFiles(patch),
      details,
      stat: diffStat(patch),
    };
  }
  return lastPatch;
}
