'use client';

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';

import { useT } from '@/components/providers';
import type { AgentJobFileApi, AgentJobFileEntryApi } from '@/lib/api/agents';

import { highlightCode, TOKEN_CLASS, type CodeToken } from '@/lib/codeHighlight';
import type { AgentJob } from './types';
import { useWorkspaceFiles, type WorkspaceDirectory } from './useAgentJobs';
import { PANE_RESIZE_EVENT } from './useResizablePane';

/** Rows rendered at once; the API already caps a preview at 512 KB. */
const MAX_PREVIEW_LINES = 4000;

/** Below this the tree and the file take turns instead of sharing the width. */
const TWO_PANE_MIN_WIDTH = 560;

type SidebarMode = 'files' | 'changes';

type FileStatus = 'added' | 'modified' | 'deleted';

function statusClass(status?: FileStatus | null): string {
  if (status === 'added') return 'text-emerald-600 dark:text-emerald-400';
  if (status === 'deleted') return 'text-red-500 dark:text-red-400';
  return 'text-amber-600 dark:text-amber-400';
}

function formatFileSize(size?: number | null): string {
  if (size === undefined || size === null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function basename(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path;
}

function dirname(path: string): string {
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

/** Directories first, then files, each alphabetical — the familiar tree order. */
function sortEntries(entries: AgentJobFileEntryApi[]): AgentJobFileEntryApi[] {
  return [...entries].sort((left, right) => {
    const leftDir = left.kind === 'directory' ? 0 : 1;
    const rightDir = right.kind === 'directory' ? 0 : 1;
    if (leftDir !== rightDir) return leftDir - rightDir;
    return left.name.localeCompare(right.name);
  });
}

function Caret({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.4}
      className={`h-3 w-3 shrink-0 text-gray-500 dark:text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

// One glyph per kind of file, the way an editor sidebar names things: a
// changelog is a clock, a lock file is a padlock. Monochrome on purpose —
// colour in this pane is reserved for the added/modified/deleted marks.
const GLYPHS = {
  info: { paths: ['M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z', 'M12 10.5v6'], dot: [12, 8] },
  clock: { paths: ['M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Z', 'M12 8v4.5l3 2'] },
  key: {
    paths: [
      'M12 4.2a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4Z',
      'M12 10.6V19.5',
      'M12 14h2.6',
      'M12 16.8h2',
    ],
  },
  gear: {
    paths: [
      'M12 8.6a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8Z',
      'M12 4v2.2M12 17.8V20M5.1 8l1.9 1.1M17 14.9l1.9 1.1M5.1 16l1.9-1.1M17 9.1 18.9 8',
    ],
  },
  lock: { paths: ['M6.5 11h11v9h-11v-9Z', 'M9 11V8a3 3 0 0 1 6 0v3'] },
  sliders: { paths: ['M4.5 7.5h15', 'M4.5 12h10', 'M4.5 16.5h6'] },
  diamond: { paths: ['M12 3.2 20.8 12 12 20.8 3.2 12 12 3.2Z'] },
  code: { paths: ['m9 8-4 4 4 4', 'm15 8 4 4-4 4'] },
  braces: {
    paths: [
      'M9.5 4.5H8a2 2 0 0 0-2 2v3l-2 2.5 2 2.5v3a2 2 0 0 0 2 2h1.5',
      'M14.5 4.5H16a2 2 0 0 1 2 2v3l2 2.5-2 2.5v3a2 2 0 0 1-2 2h-1.5',
    ],
  },
  doc: { paths: ['M7 4.5h6.5L18 9v10.5H7V4.5Z', 'M13 4.5V9h5', 'M9.5 13h5', 'M9.5 16h5'] },
  terminal: { paths: ['M4.5 5h15v14h-15V5Z', 'm8 10 2 2-2 2', 'M12.5 14h4'] },
  image: { paths: ['M4.5 6h15v12h-15V6Z', 'm7 14.5 3-3 2.5 2.5 2-2 2.5 2.5'] },
  table: { paths: ['M4.5 6h15v12h-15V6Z', 'M4.5 10.5h15', 'M10 6v12'] },
  plain: { paths: ['M7 4.5h6.5L18 9v10.5H7V4.5Z', 'M13 4.5V9h5'] },
} as const;

type GlyphName = keyof typeof GLYPHS;

/** Whole-name matches, checked as prefixes so `README.zh.md` still counts. */
const GLYPH_BY_NAME: Array<[string, GlyphName]> = [
  ['readme', 'info'],
  ['changelog', 'clock'],
  ['history', 'clock'],
  ['license', 'key'],
  ['licence', 'key'],
  ['copying', 'key'],
  ['notice', 'key'],
  ['.env', 'sliders'],
  ['.gitignore', 'diamond'],
  ['.gitattributes', 'diamond'],
  ['.dockerignore', 'diamond'],
  ['.editorconfig', 'sliders'],
  ['dockerfile', 'terminal'],
  ['makefile', 'terminal'],
];

const GLYPH_BY_EXTENSION: Record<string, GlyphName> = {
  ts: 'code',
  tsx: 'code',
  js: 'code',
  jsx: 'code',
  mjs: 'code',
  cjs: 'code',
  py: 'code',
  go: 'code',
  rs: 'code',
  rb: 'code',
  java: 'code',
  kt: 'code',
  swift: 'code',
  c: 'code',
  h: 'code',
  cc: 'code',
  cpp: 'code',
  cs: 'code',
  php: 'code',
  css: 'code',
  scss: 'code',
  html: 'code',
  vue: 'code',
  json: 'braces',
  jsonc: 'braces',
  yaml: 'braces',
  yml: 'braces',
  toml: 'gear',
  ini: 'gear',
  cfg: 'gear',
  conf: 'gear',
  properties: 'gear',
  lock: 'lock',
  pem: 'lock',
  key: 'lock',
  crt: 'lock',
  md: 'doc',
  markdown: 'doc',
  mdx: 'doc',
  rst: 'doc',
  txt: 'doc',
  log: 'doc',
  sh: 'terminal',
  bash: 'terminal',
  zsh: 'terminal',
  fish: 'terminal',
  csv: 'table',
  tsv: 'table',
  parquet: 'table',
  sql: 'table',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  svg: 'image',
  webp: 'image',
  ico: 'image',
  pdf: 'image',
};

function glyphFor(name: string): GlyphName {
  const lower = name.toLowerCase();
  const byName = GLYPH_BY_NAME.find(([prefix]) => lower.startsWith(prefix));
  if (byName) return byName[1];
  const dot = lower.lastIndexOf('.');
  return (dot > 0 ? GLYPH_BY_EXTENSION[lower.slice(dot + 1)] : undefined) ?? 'plain';
}

function FileGlyph({ name }: { name: string }) {
  const glyph = GLYPHS[glyphFor(name)];
  const dot = 'dot' in glyph ? glyph.dot : undefined;
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4 shrink-0 text-gray-500 dark:text-gray-400"
    >
      {glyph.paths.map((d) => (
        <path key={d} d={d} />
      ))}
      {dot ? <circle cx={dot[0]} cy={dot[1]} r={0.9} fill="currentColor" stroke="none" /> : null}
    </svg>
  );
}

function StatusMark({ status }: { status?: FileStatus | null }) {
  if (!status) return null;
  return (
    <span
      title={status}
      className={`shrink-0 text-[10px] font-semibold uppercase ${statusClass(status)}`}
    >
      {status.charAt(0)}
    </span>
  );
}

function TreeLevel({
  path,
  depth,
  directories,
  expanded,
  selected,
  onToggle,
  onRetry,
  onOpen,
}: {
  path: string;
  depth: number;
  directories: Record<string, WorkspaceDirectory>;
  expanded: Record<string, boolean>;
  selected: string | null;
  onToggle: (path: string) => void;
  onRetry: (path: string) => void;
  onOpen: (path: string) => void;
}) {
  const t = useT();
  const state = directories[path];
  const indent = { paddingLeft: `${0.5 + depth * 0.875}rem` };

  if (state?.error) {
    return (
      <div style={indent} className="space-y-1 py-1 pr-2 text-[11px]">
        <p role="alert" className="text-red-600 dark:text-red-400">
          {state.error}
        </p>
        <button
          type="button"
          onClick={() => onRetry(path)}
          aria-label={t('workspaceFiles.retryFolder', { path: path || '/' })}
          className="rounded px-2 py-1 text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 dark:text-gray-300 dark:hover:bg-white/10"
        >
          {t('workspaceFiles.retry')}
        </button>
      </div>
    );
  }
  if (!state?.entries) {
    return state?.loading ? (
      <p style={indent} className="py-1 pr-2 text-[11px] text-gray-400 dark:text-gray-500">
        Loading…
      </p>
    ) : null;
  }
  if (state.entries.length === 0) {
    return (
      <p style={indent} className="py-1 pr-2 text-[11px] text-gray-400 dark:text-gray-500">
        Empty
      </p>
    );
  }

  return (
    <>
      {sortEntries(state.entries).map((entry) => {
        const isDirectory = entry.kind === 'directory';
        const open = Boolean(expanded[entry.path]);
        const active = selected === entry.path;
        return (
          <div key={entry.path}>
            <button
              type="button"
              onClick={() => (isDirectory ? onToggle(entry.path) : onOpen(entry.path))}
              aria-expanded={isDirectory ? open : undefined}
              aria-current={active ? 'true' : undefined}
              aria-label={entry.path}
              data-workspace-file={isDirectory ? undefined : entry.path}
              title={entry.path}
              style={indent}
              className={`ui-focus-ring flex min-h-8 w-full items-center gap-2 rounded-md py-[5px] pr-2 text-left text-[13px] ${
                active
                  ? 'bg-gray-200/60 dark:bg-white/10 text-gray-900 dark:text-gray-50'
                  : 'text-gray-800 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-white/10'
              }`}
            >
              {isDirectory ? <Caret open={open} /> : <FileGlyph name={entry.name} />}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              <StatusMark status={entry.status ?? null} />
            </button>
            {isDirectory && open ? (
              <TreeLevel
                path={entry.path}
                depth={depth + 1}
                directories={directories}
                expanded={expanded}
                selected={selected}
                onToggle={onToggle}
                onRetry={onRetry}
                onOpen={onOpen}
              />
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function ChangedList({
  job,
  selected,
  onOpen,
}: {
  job: AgentJob;
  selected: string | null;
  onOpen: (path: string) => void;
}) {
  const details = job.diffFileDetails ?? [];
  const rows = job.diffFiles.map((path) => ({
    path,
    stat: details.find((file) => file.path === path),
  }));

  if (rows.length === 0) {
    return (
      <p className="px-3 py-4 text-[11px] text-gray-400 dark:text-gray-500">
        This run changed no files.
      </p>
    );
  }

  return (
    <>
      {rows.map((row) => (
        <button
          key={row.path}
          type="button"
          onClick={() => onOpen(row.path)}
          aria-label={row.path}
          aria-current={selected === row.path ? 'true' : undefined}
          data-workspace-file={row.path}
          title={row.path}
          className={`ui-focus-ring flex min-h-10 w-full items-center gap-2 rounded-md py-2 pl-2 pr-2 text-left text-[13px] ${
            selected === row.path
              ? 'bg-gray-200/60 dark:bg-white/10 text-gray-900 dark:text-gray-50'
              : 'text-gray-800 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-white/10'
          }`}
        >
          <FileGlyph name={basename(row.path)} />
          <span className="min-w-0 flex-1">
            <span className="block break-all">{basename(row.path)}</span>
            {dirname(row.path) ? (
              <span className="mt-0.5 block break-all text-[11px] text-muted">
                {dirname(row.path)}
              </span>
            ) : null}
          </span>
          {row.stat ? (
            <span aria-hidden="true" className="shrink-0 font-mono text-[10px] text-muted">
              <span className="text-emerald-600 dark:text-emerald-400">+{row.stat.add}</span>{' '}
              <span className="text-red-500 dark:text-red-400">−{row.stat.del}</span>
            </span>
          ) : null}
        </button>
      ))}
    </>
  );
}

/** One line of tokens; plain runs stay bare text so lines read as one string. */
function TokenLine({ tokens }: { tokens: CodeToken[] }) {
  return (
    <>
      {tokens.map((token, index) =>
        token.kind === 'plain' ? (
          token.text
        ) : (
          <span key={index} className={TOKEN_CLASS[token.kind]}>
            {token.text}
          </span>
        ),
      )}
    </>
  );
}

function CodeView({ path, content }: { path: string; content: string }) {
  const lines = useMemo(
    () =>
      highlightCode(content, path).map((tokens) => ({
        tokens,
        // A token too long to fit (a URL, a hash) has to break mid-token, or
        // the browser breaks at the indent instead and leaves the row visibly
        // empty. Ordinary lines keep wrapping on word boundaries.
        breakAnywhere: /\S{60,}/.test(tokens.map((token) => token.text).join('')),
      })),
    [content, path],
  );
  const shown = lines.slice(0, MAX_PREVIEW_LINES);

  return (
    <div className="py-2 font-mono text-[12px] leading-[1.7]">
      {shown.map(({ tokens, breakAnywhere }, index) => (
        // The line number *is* the identity here, so the index is a valid key.
        <div key={index} className="flex hover:bg-gray-50 dark:hover:bg-white/5">
          <span
            aria-hidden="true"
            className="w-12 shrink-0 select-none pr-4 text-right text-gray-300 dark:text-gray-600"
          >
            {index + 1}
          </span>
          {/* Wrapped continuations hang under the line, as an editor would. */}
          <span
            className={`min-w-0 flex-1 whitespace-pre-wrap pl-6 pr-4 -indent-6 text-gray-700 dark:text-gray-300 ${
              breakAnywhere ? 'break-all' : 'break-words'
            }`}
          >
            {tokens.length ? <TokenLine tokens={tokens} /> : ' '}
          </span>
        </div>
      ))}
      {lines.length > shown.length ? (
        <p className="px-4 py-3 text-[11px] text-gray-400 dark:text-gray-500">
          Preview limited to the first {MAX_PREVIEW_LINES.toLocaleString()} lines.
        </p>
      ) : null}
    </div>
  );
}

function PaneMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center px-6 py-16 text-center text-sm text-gray-400 dark:text-gray-500">
      {children}
    </div>
  );
}

const PREVIEW_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

/** Keep authorized bytes local to this pane, and release them on navigation. */
function useFileAssets(file: AgentJobFileApi | null) {
  const [assets, setAssets] = useState<{
    file: AgentJobFileApi;
    download: string;
    preview: string | null;
  } | null>(null);

  useEffect(() => {
    if (
      !file ||
      file.truncated ||
      file.status === 'deleted' ||
      typeof URL.createObjectURL !== 'function'
    ) {
      setAssets(null);
      return;
    }
    let body: BlobPart;
    if (file.content_base64 != null) {
      try {
        body = Uint8Array.from(atob(file.content_base64), (char) => char.charCodeAt(0));
      } catch {
        setAssets(null);
        return;
      }
    } else if (!file.binary && file.content != null) {
      body = file.content;
    } else {
      setAssets(null);
      return;
    }
    // Downloads deliberately carry no active document media type: an HTML
    // or SVG file is useful to save, but must not execute in this UI's origin.
    const download = URL.createObjectURL(new Blob([body], { type: 'application/octet-stream' }));
    const preview =
      file.content_base64 != null && file.media_type && PREVIEW_IMAGE_TYPES.has(file.media_type)
        ? URL.createObjectURL(new Blob([body], { type: file.media_type }))
        : null;
    setAssets({ file, download, preview });
    return () => {
      URL.revokeObjectURL(download);
      if (preview) URL.revokeObjectURL(preview);
    };
  }, [file]);

  return assets?.file === file ? assets : null;
}

function ImagePreview({ path, url }: { path: string; url: string }) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  return failed ? (
    <PaneMessage>{t('workspaceFiles.imageFailed')}</PaneMessage>
  ) : (
    <div className="flex min-h-0 items-center justify-center p-4">
      {/* The source is a bounded authorized Blob, not a remote image to optimize. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={t('workspaceFiles.previewAlt', { name: basename(path) })}
        onError={() => setFailed(true)}
        className="max-h-[28rem] max-w-full rounded object-contain"
      />
    </div>
  );
}

/**
 * Two-pane workspace browser: a lazily expanded tree beside a file viewer.
 *
 * Read-only: the workspace write endpoint exists, but changing a run's files
 * by hand belongs in the conversation, not in this pane. Every successful
 * response comes from the retained container's current workspace.
 */
/** How long a run of diff additions must go quiet before the tree reloads. */
const DIFF_SETTLE_MS = 1500;

/**
 * The diff count, but only once it stops moving.
 *
 * The agent adds files to the diff one at a time, and this count is part of
 * the workspace refresh key — so every single addition used to reload the
 * root, every expanded directory and the open file, each its own request over
 * a link where one round trip is several hundred milliseconds. A turn touching
 * twenty files with four directories open issued around a hundred and twenty.
 *
 * Collapsing a burst into one reload leaves the tree briefly stale while the
 * agent is mid-write, which is the right trade: `job.state` is also in the key,
 * so a settled turn still reloads immediately regardless of this delay.
 */
export function useSettledCount(value: number, delayMs: number = DIFF_SETTLE_MS): number {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    if (settled === value) return;
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs, settled]);
  return settled;
}

export function WorkspaceFiles({
  job,
  active,
  requestedPath,
  requestToken,
}: {
  job: AgentJob;
  active: boolean;
  requestedPath?: string | null;
  /** Increment to reopen the same path after a new conversation action. */
  requestToken?: number;
}) {
  const t = useT();
  const settledDiffCount = useSettledCount(job.diffFiles.length);
  const {
    directories,
    expanded,
    selected,
    directoryPath,
    file,
    fileLoading,
    fileError,
    source,
    toggleDirectory,
    retryDirectory,
    openFile,
    retryFile,
    showRootDirectory,
    closeFile,
    reload,
  } = useWorkspaceFiles(job.id, active, `${job.state}:${settledDiffCount}`);
  const assets = useFileAssets(file?.kind === 'file' ? file : null);
  const processedRequest = useRef<{
    jobId: string;
    path: string;
    token: number | undefined;
  } | null>(null);

  useEffect(() => {
    if (!requestedPath) {
      processedRequest.current = null;
      return;
    }
    if (!active) return;
    const previous = processedRequest.current;
    if (
      previous?.jobId === job.id &&
      previous.path === requestedPath &&
      previous.token === requestToken
    ) {
      return;
    }
    processedRequest.current = { jobId: job.id, path: requestedPath, token: requestToken };
    openFile(requestedPath);
  }, [active, job.id, requestedPath, requestToken, openFile]);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [paneWidth, setPaneWidth] = useState(0);
  const [mode, setMode] = useState<SidebarMode>('files');
  const listId = useId();
  const [pathInput, setPathInput] = useState('');
  const [copying, setCopying] = useState(false);
  const [copyNotice, setCopyNotice] = useState<{ error: boolean; text: string } | null>(null);
  // Clipboard promises belong to the selection they started from.
  const copyScope = useMemo(() => ({ jobId: job.id, selected, file }), [job.id, selected, file]);
  const currentCopyScope = useRef(copyScope);
  currentCopyScope.current = copyScope;
  const mounted = useRef(true);
  const paneRef = useRef<HTMLElement | null>(null);
  const previewRef = useRef<HTMLDivElement | null>(null);
  const tabRefs = useRef<Partial<Record<SidebarMode, HTMLButtonElement | null>>>({});
  const focusPreview = useRef(false);
  const returnPath = useRef<string | null>(null);
  const focusList = useRef(false);

  useEffect(() => {
    setCopyNotice(null);
    setCopying(false);
  }, [copyScope]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function copy(value: string, kind: 'path' | 'contents') {
    const scope = currentCopyScope.current;
    setCopyNotice(null);
    setCopying(true);
    try {
      await navigator.clipboard.writeText(value);
      if (mounted.current && currentCopyScope.current === scope)
        setCopyNotice({
          error: false,
          text: t(kind === 'path' ? 'workspaceFiles.pathCopied' : 'workspaceFiles.contentsCopied'),
        });
    } catch {
      if (mounted.current && currentCopyScope.current === scope)
        setCopyNotice({ error: true, text: t('workspaceFiles.copyFailed') });
    } finally {
      if (mounted.current && currentCopyScope.current === scope) setCopying(false);
    }
  }

  const root = directories[''];
  // The workspace pane is user-resizable, so whether there is room for two
  // panes is a fact about this box and not about the viewport. Unmeasured (SSR,
  // jsdom) counts as roomy: the two-pane layout is the normal one.
  const narrow = paneWidth > 0 && paneWidth < TWO_PANE_MIN_WIDTH;
  // When it is not, the tree and the file take turns and the header button
  // walks back to the tree.
  const showTree = sidebarOpen && !(narrow && selected);

  function chooseFile(path: string) {
    returnPath.current = path;
    focusPreview.current = narrow;
    openFile(path);
  }

  function returnToList() {
    returnPath.current = selected;
    focusList.current = true;
    setSidebarOpen(true);
    closeFile();
  }

  useEffect(() => {
    if (!active) return;
    if (narrow && focusPreview.current && !fileLoading) {
      focusPreview.current = false;
      if (selected) previewRef.current?.focus({ preventScroll: true });
      // A typed path can open a folder instead of a preview. Keep keyboard
      // focus in the visible list after its contents replace the pending read.
      else focusList.current = true;
    }
    if (showTree && focusList.current) {
      focusList.current = false;
      const panel = document.getElementById(`${listId}-panel-${mode}`);
      const row = Array.from(
        panel?.querySelectorAll<HTMLButtonElement>('[data-workspace-file]') ?? [],
      ).find((button) => button.dataset.workspaceFile === returnPath.current);
      (row ?? tabRefs.current[mode])?.focus({ preventScroll: true });
      row?.scrollIntoView?.({ block: 'nearest' });
    }
  }, [active, fileLoading, listId, mode, narrow, selected, showTree]);

  const attachPane = useCallback((node: HTMLElement | null) => {
    paneRef.current = node;
    if (node) setPaneWidth(node.clientWidth);
  }, []);

  useEffect(() => {
    const measure = () => {
      if (paneRef.current) setPaneWidth(paneRef.current.clientWidth);
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener(PANE_RESIZE_EVENT, measure);
    const observer =
      typeof ResizeObserver === 'undefined' || !paneRef.current
        ? null
        : new ResizeObserver(measure);
    observer?.observe(paneRef.current as HTMLElement);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener(PANE_RESIZE_EVENT, measure);
      observer?.disconnect();
    };
  }, []);

  const sourceLabel = directories[directoryPath ?? '']?.error
    ? 'Unavailable'
    : (file?.source ?? source) === 'workspace'
      ? null
      : 'Loading…';

  return (
    <section
      ref={attachPane}
      aria-label="Workspace files"
      className="flex h-[36rem] max-h-[calc(100vh-11rem)] flex-col overflow-hidden rounded-xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900"
    >
      <div className="flex min-h-14 shrink-0 items-center gap-2 border-b border-border bg-surface-muted px-2.5 py-2">
        {narrow && selected ? (
          <button
            type="button"
            onClick={returnToList}
            aria-label="Back to file list"
            className="ui-button ui-button-ghost shrink-0 px-2 text-xs"
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" className="h-4 w-4">
              <path
                d="m14 6-6 6 6 6"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Back
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={showTree ? 'Hide file tree' : 'Show file tree'}
            aria-pressed={showTree}
            className={`ui-icon-button ui-button-ghost shrink-0 ${showTree ? 'bg-surface-hover text-foreground' : ''}`}
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              className="h-4 w-4"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
        )}

        {selected ? (
          <span className="min-w-0 flex-1" title={selected}>
            <span className="block break-all text-[13px] font-medium text-foreground">
              {basename(selected)}
            </span>
            {dirname(selected) ? (
              <span className="mt-0.5 block break-all text-[11px] text-muted">
                {dirname(selected)}
              </span>
            ) : null}
          </span>
        ) : (
          <span className="text-[13px] text-gray-500 dark:text-gray-400">Files</span>
        )}

        {sourceLabel ? (
          <span className="ml-auto shrink-0 rounded bg-white dark:bg-gray-900 px-2 py-0.5 text-[11px] text-gray-400 dark:text-gray-500 ring-1 ring-gray-200 dark:ring-white/10">
            {sourceLabel}
          </span>
        ) : null}
        {selected ? (
          <button
            type="button"
            disabled={copying}
            onClick={() => void copy(selected, 'path')}
            className="ml-auto shrink-0 rounded px-2 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/10"
          >
            {t('workspaceFiles.copyPath')}
          </button>
        ) : null}
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!pathInput || !active) return;
          setSidebarOpen(true);
          setMode('files');
          chooseFile(pathInput);
        }}
        className="flex shrink-0 items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-white/10"
      >
        <input
          type="text"
          value={pathInput}
          onChange={(event) => setPathInput(event.target.value)}
          aria-label={t('workspaceFiles.pathLabel')}
          placeholder={t('workspaceFiles.pathPlaceholder')}
          maxLength={4096}
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 rounded-md border border-gray-200 bg-transparent px-2 py-1.5 font-mono text-xs text-gray-800 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-indigo-500/30 dark:border-white/10 dark:text-gray-100 dark:placeholder:text-gray-500"
        />
        <button
          type="submit"
          disabled={!pathInput || !active}
          className="shrink-0 rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-50 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/10"
        >
          {t('workspaceFiles.openPath')}
        </button>
      </form>
      {copyNotice ? (
        <p
          role={copyNotice.error ? 'alert' : 'status'}
          className={`shrink-0 px-3 py-1 text-[11px] ${copyNotice.error ? 'text-red-600 dark:text-red-400' : 'text-gray-600 dark:text-gray-300'}`}
        >
          {copyNotice.text}
        </p>
      ) : null}

      <div className="flex min-h-0 flex-1">
        {showTree ? (
          <aside
            className={`flex shrink-0 flex-col border-r border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 ${
              narrow ? 'w-full' : 'w-56'
            }`}
          >
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-2 text-[12px]">
              <div
                role="tablist"
                aria-label="File list"
                className="flex min-w-0 items-center gap-1"
              >
                {(
                  [
                    ['files', 'Files'],
                    ['changes', 'Changes'],
                  ] as Array<[SidebarMode, string]>
                ).map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    ref={(node) => {
                      tabRefs.current[key] = node;
                    }}
                    id={`${listId}-tab-${key}`}
                    role="tab"
                    aria-selected={mode === key}
                    aria-controls={`${listId}-panel-${key}`}
                    tabIndex={mode === key ? 0 : -1}
                    onClick={() => setMode(key)}
                    onKeyDown={(event) => {
                      const next =
                        event.key === 'Home'
                          ? 'files'
                          : event.key === 'End'
                            ? 'changes'
                            : ['ArrowLeft', 'ArrowRight'].includes(event.key)
                              ? key === 'files'
                                ? 'changes'
                                : 'files'
                              : null;
                      if (!next) return;
                      event.preventDefault();
                      setMode(next);
                      tabRefs.current[next]?.focus();
                    }}
                    className="ui-tab min-h-10 px-2 font-medium"
                  >
                    {label}
                    {key === 'changes' && job.diffFiles.length ? (
                      <span className="ml-1 text-[10px] text-gray-400 dark:text-gray-500">
                        {job.diffFiles.length}
                      </span>
                    ) : null}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={reload}
                aria-label="Refresh files"
                className="ui-icon-button ui-button-ghost ml-auto"
              >
                <svg
                  aria-hidden="true"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.8}
                  className="h-3.5 w-3.5"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M4 12a8 8 0 0 1 13.7-5.6L20 8m0-4v4h-4M20 12a8 8 0 0 1-13.7 5.6L4 16m0 4v-4h4"
                  />
                </svg>
              </button>
            </div>
            {directoryPath && mode === 'files' ? (
              <div className="flex shrink-0 items-center gap-2 border-b border-gray-200 px-2 py-1.5 text-[11px] dark:border-white/10">
                <button
                  type="button"
                  onClick={showRootDirectory}
                  className="shrink-0 rounded px-1 py-1 text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 dark:text-gray-300 dark:hover:bg-white/10"
                >
                  {t('workspaceFiles.root')}
                </button>
                <span
                  title={directoryPath}
                  className="min-w-0 truncate font-mono text-gray-500 dark:text-gray-400"
                >
                  {directoryPath}
                </span>
              </div>
            ) : null}
            <div
              id={`${listId}-panel-files`}
              role="tabpanel"
              aria-labelledby={`${listId}-tab-files`}
              hidden={mode !== 'files'}
              className="min-h-0 flex-1 overflow-auto px-1 py-1"
            >
              {mode === 'files' ? (
                <TreeLevel
                  path={directoryPath ?? ''}
                  depth={0}
                  directories={directories}
                  expanded={expanded}
                  selected={selected}
                  onToggle={toggleDirectory}
                  onRetry={retryDirectory}
                  onOpen={chooseFile}
                />
              ) : null}
            </div>
            <div
              id={`${listId}-panel-changes`}
              role="tabpanel"
              aria-labelledby={`${listId}-tab-changes`}
              hidden={mode !== 'changes'}
              className="min-h-0 flex-1 overflow-auto px-1 py-1"
            >
              {mode === 'changes' ? (
                <ChangedList job={job} selected={selected} onOpen={chooseFile} />
              ) : null}
            </div>
          </aside>
        ) : null}

        <div
          ref={previewRef}
          role="region"
          aria-label={selected ? `Preview of ${selected}` : 'File preview'}
          aria-busy={fileLoading}
          tabIndex={selected ? 0 : -1}
          onKeyDown={(event) => {
            if (narrow && selected && event.key === 'Escape' && !event.defaultPrevented) {
              event.preventDefault();
              event.stopPropagation();
              returnToList();
            }
          }}
          className={`ui-focus-ring min-w-0 flex-1 overflow-auto bg-surface -outline-offset-2 ${showTree && narrow ? 'hidden' : ''}`}
        >
          {fileLoading ? (
            <PaneMessage>Loading file…</PaneMessage>
          ) : fileError ? (
            <PaneMessage>
              <span className="flex flex-col items-center gap-3">
                <span role="alert" className="text-red-600 dark:text-red-400">
                  {fileError}
                </span>
                <button
                  type="button"
                  onClick={retryFile}
                  aria-label={t('workspaceFiles.retryFile')}
                  className="rounded-md border border-gray-200 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/5"
                >
                  {t('workspaceFiles.retry')}
                </button>
              </span>
            </PaneMessage>
          ) : !selected || !file ? (
            <PaneMessage>
              {directoryPath === null && root?.error ? (
                <span className="flex flex-col items-center gap-3">
                  <span role="alert" className="text-red-600 dark:text-red-400">
                    {root.error}
                  </span>
                  <button
                    type="button"
                    onClick={() => retryDirectory('')}
                    className="rounded-md border border-gray-200 dark:border-white/10 px-3 py-1.5 text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5"
                  >
                    {t('workspaceFiles.retry')}
                  </button>
                </span>
              ) : (
                'Select a file to preview it here.'
              )}
            </PaneMessage>
          ) : file.kind === 'symlink' ? (
            <PaneMessage>Symlinks are not previewed.</PaneMessage>
          ) : file.status === 'deleted' ? (
            <PaneMessage>This file was deleted by the run.</PaneMessage>
          ) : (
            <>
              <div className="flex items-center gap-2 border-b border-gray-100 dark:border-white/5 px-4 py-1.5 text-[11px] text-gray-400 dark:text-gray-500">
                <span>{formatFileSize(file.size)}</span>
                {file.status ? (
                  <span className={`font-medium ${statusClass(file.status)}`}>{file.status}</span>
                ) : null}
                {file.truncated ? <span>Preview truncated</span> : null}
                {!file.binary && !file.truncated && file.content !== null ? (
                  <button
                    type="button"
                    disabled={copying}
                    onClick={() => void copy(file.content!, 'contents')}
                    className="ml-auto rounded px-2 py-1 font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/10"
                  >
                    {t('workspaceFiles.copyContents')}
                  </button>
                ) : null}
                {assets?.download ? (
                  <a
                    href={assets.download}
                    download={basename(file.path)}
                    className="ml-auto rounded px-2 py-1 font-medium text-gray-600 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500 dark:text-gray-300 dark:hover:bg-white/10"
                  >
                    {t('workspaceFiles.download')}
                  </a>
                ) : null}
              </div>
              {file.truncated && file.content == null ? (
                <PaneMessage>{t('workspaceFiles.tooLarge')}</PaneMessage>
              ) : assets?.preview ? (
                <ImagePreview key={assets.preview} path={file.path} url={assets.preview} />
              ) : file.binary ? (
                <PaneMessage>
                  {assets?.download
                    ? t('workspaceFiles.binaryPreview')
                    : t('workspaceFiles.binaryMissing')}
                </PaneMessage>
              ) : (
                <CodeView path={file.path} content={file.content ?? ''} />
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
