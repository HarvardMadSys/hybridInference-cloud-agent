'use client';

import { useT } from '@/components/providers';
import { Select } from '@/components/ui/Select';
import type { AgentGitWorkspaceApi } from '@/lib/api/agents';
import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import { memo, useEffect, useId, useMemo, useRef, useState } from 'react';
import { ownerFailureMessage, toDiffFileDetails } from '../adapt';
import type { AgentDiffFile, AgentJob } from '../types';
import { loadWorkspaceGit } from '../workspaceGitCache';
import { ReviewButton, WatchPullRequestButton } from './RunOutcome';

/** Rows drawn at once, as in the file preview: a lockfile's diff runs to tens of thousands. */
const MAX_DIFF_LINES = 4000;

const DIFF_LINE_CLASS = {
  hunk: 'bg-gray-50 dark:bg-gray-950 py-1 text-gray-400 dark:text-gray-500',
  ctx: 'text-gray-600 dark:text-gray-400',
  add: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  del: 'bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400',
} as const;

type GitView = 'diff' | 'review' | 'commits';

function DiffLinesView({ file }: { file: AgentDiffFile }) {
  const region = useRef<HTMLPreElement>(null);
  // Rows the owner has asked to see, MAX_DIFF_LINES more at a time. Another
  // file, or a new diff of this one, starts again from its first rows.
  const [drawn, setDrawn] = useState({ file, rows: MAX_DIFF_LINES });
  const current = drawn.file === file;
  if (!current) setDrawn({ file, rows: MAX_DIFF_LINES });
  const shown = file.lines.slice(0, current ? drawn.rows : MAX_DIFF_LINES);
  const remaining = file.lines.length - shown.length;
  return (
    <>
      <pre
        ref={region}
        tabIndex={0}
        role="region"
        aria-label={`Diff for ${file.path}`}
        className="ui-focus-ring max-h-[34rem] overflow-auto font-mono text-xs leading-relaxed"
      >
        <code>
          {shown.map((line, index) => (
            <span key={index} className={`block min-w-max px-4 ${DIFF_LINE_CLASS[line.marker]}`}>
              {line.text || ' '}
            </span>
          ))}
        </code>
      </pre>
      {remaining > 0 ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 text-[11px] text-gray-400 dark:text-gray-500">
          <p>
            Diff limited to the first {shown.length.toLocaleString()} of{' '}
            {file.lines.length.toLocaleString()} lines.
          </p>
          <button
            type="button"
            onClick={() => {
              // The last rows take this button away; the reader stays in the diff.
              if (remaining <= MAX_DIFF_LINES) region.current?.focus({ preventScroll: true });
              setDrawn({ file, rows: shown.length + MAX_DIFF_LINES });
            }}
            className="ui-focus-ring rounded-md border border-gray-200 dark:border-white/10 px-2.5 py-1 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5"
          >
            {remaining > MAX_DIFF_LINES
              ? `Show ${MAX_DIFF_LINES.toLocaleString()} more lines`
              : `Show the remaining ${remaining.toLocaleString()} line${remaining === 1 ? '' : 's'}`}
          </button>
        </div>
      ) : null}
    </>
  );
}

// The selected file's lines, drawn again only when another file or diff is
// shown, or more of it is asked for — not for a refresh, a view switch, or the
// page around the panel.
const DiffLines = memo(DiffLinesView);

export function GitPanel({
  job,
  active,
  onReview,
  onWatchPr,
}: {
  job: Pick<
    AgentJob,
    | 'id'
    | 'threadId'
    | 'sandboxId'
    | 'repo'
    | 'branch'
    | 'baseRef'
    | 'baseSha'
    | 'prUrl'
    | 'diffFileDetails'
    | 'workspaceGitChanges'
    | 'workspaceGitRevision'
    | 'workspaceGitMetadataPending'
    | 'workspaceGitAvailable'
  >;
  active: boolean;
  onReview?: () => void;
  onWatchPr?: () => void;
}) {
  const t = useT();
  const [view, setView] = useState<GitView>('diff');
  const gitId = useId();
  const [live, setLive] = useState<AgentGitWorkspaceApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const appliedRefresh = useRef(0);
  const gitTarget = useMemo(
    () => ({
      id: job.id,
      sandboxId: job.sandboxId,
      threadId: job.threadId,
      workspaceGitRevision: job.workspaceGitRevision,
    }),
    [job.id, job.sandboxId, job.threadId, job.workspaceGitRevision],
  );
  useEffect(() => {
    if (!active || job.workspaceGitMetadataPending) return undefined;
    if (job.workspaceGitAvailable === false) {
      setLive({
        available: false,
        revision: job.workspaceGitRevision,
        branch: job.branch,
        changes: job.workspaceGitChanges ?? [],
        change_count: job.workspaceGitChanges?.length ?? 0,
        patch: '',
        commits: [],
      });
      setLoading(false);
      setError(null);
      return undefined;
    }
    let cancelled = false;
    const refresh = refreshKey > appliedRefresh.current;
    if (refresh) appliedRefresh.current = refreshKey;
    setLoading(true);
    setError(null);
    loadWorkspaceGit(gitTarget, { refresh })
      .then((result) => {
        if (!cancelled) setLive(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setLive(null);
          setError(cause instanceof Error ? cause.message : 'Could not load Git workspace');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    active,
    gitTarget,
    job.branch,
    job.workspaceGitAvailable,
    job.workspaceGitChanges,
    job.workspaceGitMetadataPending,
    job.workspaceGitRevision,
    refreshKey,
  ]);

  const files = useMemo(
    () => (live?.available ? toDiffFileDetails(live.patch) : (job.diffFileDetails ?? [])),
    [job.diffFileDetails, live],
  );
  const [selectedPath, setSelectedPath] = useState(files[0]?.path ?? '');
  const selected = files.find((file) => file.path === selectedPath) ?? files[0];
  const stat = useMemo(
    () =>
      files.reduce((total, file) => ({ add: total.add + file.add, del: total.del + file.del }), {
        add: 0,
        del: 0,
      }),
    [files],
  );

  useEffect(() => {
    if (files.length && !files.some((file) => file.path === selectedPath)) {
      setSelectedPath(files[0].path);
    }
  }, [files, selectedPath]);

  return (
    <div className="agent-git-workspace min-w-0 space-y-4" aria-label="Git workspace">
      <header className="flex flex-wrap items-center gap-3 border-b border-gray-200 dark:border-white/10 pb-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
            {isTemporaryWorkspace(job.repo) ? t('temporaryWorkspace.name') : job.repo}
          </p>
          <p className="truncate font-mono text-xs text-gray-400 dark:text-gray-500">
            {live?.available ? live.branch || 'detached HEAD' : job.branch}
          </p>
        </div>
        {/* Silent while the workspace is still answering: a badge that appears
            only to vanish reads as a fault. The Refresh button carries the wait. */}
        {error || (!loading && !live?.available) ? (
          <span className="rounded bg-gray-100 dark:bg-white/5 px-2 py-1 text-[11px] text-gray-500 dark:text-gray-400">
            Unavailable
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => setRefreshKey((value) => value + 1)}
          disabled={loading}
          className="ml-auto rounded-md border border-gray-200 dark:border-white/10 px-2.5 py-1 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-50"
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <div
        className="flex items-center gap-5 border-b border-gray-200 dark:border-white/10"
        role="tablist"
        aria-label="Git views"
      >
        {(['diff', 'review', 'commits'] as GitView[]).map((item, index, views) => (
          <button
            key={item}
            type="button"
            role="tab"
            id={`${gitId}-tab-${item}`}
            aria-controls={`${gitId}-panel-${item}`}
            aria-selected={view === item}
            tabIndex={view === item ? 0 : -1}
            onClick={() => setView(item)}
            onKeyDown={(event) => {
              const next =
                event.key === 'ArrowRight'
                  ? (index + 1) % views.length
                  : event.key === 'ArrowLeft'
                    ? (index + views.length - 1) % views.length
                    : event.key === 'Home'
                      ? 0
                      : event.key === 'End'
                        ? views.length - 1
                        : null;
              if (next === null) return;
              event.preventDefault();
              setView(views[next]);
              document.getElementById(`${gitId}-tab-${views[next]}`)?.focus();
            }}
            className={`ui-tab min-h-9 px-2 py-2 text-xs font-medium capitalize ${
              view === item
                ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-gray-50'
                : 'border-transparent text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200'
            }`}
          >
            {item}
          </button>
        ))}
        {view === 'diff' && files.length ? (
          <span className="ml-auto pb-2 font-mono text-[11px]">
            <span className="text-emerald-600 dark:text-emerald-400">+{stat.add}</span>{' '}
            <span className="text-red-500 dark:text-red-400">−{stat.del}</span>
          </span>
        ) : null}
      </div>

      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400">{ownerFailureMessage(error)}</p>
      ) : null}

      <div id={`${gitId}-panel-${view}`} role="tabpanel" aria-labelledby={`${gitId}-tab-${view}`}>
        {view === 'diff' && files.length ? (
          <section className="agent-git-diff overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
            <div className="agent-git-file-list border-b border-gray-200 dark:border-white/10 bg-gray-50/60 dark:bg-gray-900/60">
              <div className="flex items-center px-3 py-2.5 text-xs text-gray-500 dark:text-gray-400">
                <span>
                  {files.length} changed file{files.length === 1 ? '' : 's'}
                </span>
                <span className="ml-auto font-mono">Changes</span>
              </div>
              <div className="max-h-[34rem] overflow-auto border-t border-gray-200 dark:border-white/10">
                {files.map((file) => (
                  <button
                    key={file.path}
                    type="button"
                    onClick={() => setSelectedPath(file.path)}
                    aria-pressed={selected?.path === file.path}
                    title={file.path}
                    className={`ui-focus-ring flex w-full items-center gap-3 border-b border-gray-100 dark:border-white/5 px-3 py-2.5 text-left text-xs last:border-b-0 ${
                      selected?.path === file.path
                        ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-50'
                        : 'text-gray-600 dark:text-gray-400 hover:bg-white dark:hover:bg-white/10'
                    }`}
                  >
                    <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                    <span className="shrink-0 font-mono">
                      <span className="text-emerald-600 dark:text-emerald-400">+{file.add}</span>{' '}
                      <span className="text-red-500 dark:text-red-400">−{file.del}</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <div className="min-w-0 bg-white dark:bg-gray-900">
              <label className="agent-git-file-picker border-b border-border bg-surface-muted px-3 py-2 text-xs text-muted">
                <span>
                  {files.length} changed file{files.length === 1 ? '' : 's'}
                </span>
                <Select
                  aria-label="Changed file"
                  value={selected?.path ?? ''}
                  onValueChange={setSelectedPath}
                  className="ui-input mt-1 block w-full min-w-0 py-2 font-mono text-xs"
                  options={files.map((file) => ({
                    value: file.path,
                    label: `${file.path} (+${file.add} −${file.del})`,
                  }))}
                />
              </label>
              <div className="agent-git-selected-path border-b border-gray-100 dark:border-white/5 px-4 py-2.5 font-mono text-xs text-gray-600 dark:text-gray-400">
                {selected?.path}
              </div>
              {selected ? <DiffLines file={selected} /> : null}
            </div>
          </section>
        ) : view === 'diff' ? (
          <div className="rounded-xl border border-dashed border-gray-200 dark:border-white/10 px-5 py-12 text-center text-sm text-gray-400 dark:text-gray-500">
            No changes yet.
          </div>
        ) : view === 'review' ? (
          <section className="space-y-4 rounded-xl border border-gray-200 dark:border-white/10 p-5">
            <div className="flex items-center gap-3">
              <div>
                <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                  {files.length} changed file{files.length === 1 ? '' : 's'}
                </p>
                <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
                  Compared with {job.baseRef || job.baseSha || 'the pinned base'}
                </p>
              </div>
              <p className="ml-auto font-mono text-xs">
                <span className="text-emerald-600 dark:text-emerald-400">+{stat.add}</span>{' '}
                <span className="text-red-500 dark:text-red-400">−{stat.del}</span>
              </p>
            </div>
            {files.length ? (
              <ul className="divide-y divide-gray-100 dark:divide-white/5 border-y border-gray-100 dark:border-white/5">
                {files.map((file) => (
                  <li key={file.path} className="flex gap-3 py-2.5 font-mono text-xs">
                    <span className="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-400">
                      {file.path}
                    </span>
                    <span className="text-emerald-600 dark:text-emerald-400">+{file.add}</span>
                    <span className="text-red-500 dark:text-red-400">−{file.del}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {job.prUrl ? (
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={job.prUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-xs font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
                >
                  Open pull request
                </a>
                <ReviewButton onReview={onReview} />
                <WatchPullRequestButton onWatchPr={onWatchPr} />
              </div>
            ) : (
              <p className="text-xs text-gray-400 dark:text-gray-500">
                Pull request not published yet.
              </p>
            )}
          </section>
        ) : (
          <section className="overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
            {live?.available && live.commits.length ? (
              <ul className="divide-y divide-gray-100 dark:divide-white/5">
                {live.commits.map((commit) => (
                  <li key={commit.sha} className="px-4 py-3">
                    <div className="flex items-start gap-3">
                      <span className="font-mono text-xs text-gray-400 dark:text-gray-500">
                        {commit.short_sha}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate text-sm text-gray-700 dark:text-gray-300">
                          {commit.subject}
                        </p>
                        <p className="mt-1 text-[11px] text-gray-400 dark:text-gray-500">
                          {commit.author} · {commit.authored_at}
                        </p>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 py-12 text-center text-sm text-gray-400 dark:text-gray-500">
                Commit history is available for live workspaces.
              </p>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
