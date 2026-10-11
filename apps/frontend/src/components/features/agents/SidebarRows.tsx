import Link from 'next/link';
import { useEffect, useState, type HTMLAttributes } from 'react';
import { useT } from '@/components/providers';
import { projectLabel } from './conversations';
import { CONVERSATION_STATE_KEYS, getConversationState } from './conversationState';
import { activityLabel } from './relativeTime';
import { useConversationList, type SidebarConversation } from './useConversationList';
import { useSessionReview } from './useSessionReview';
import { SessionReviewBadge } from './SessionReviewBadge';
export function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const pieces = [];
  let offset = 0;
  // Match against the original string: Unicode case conversion can change its length.
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(escaped, 'giu');
  for (const match of text.matchAll(pattern)) {
    const index = match.index;
    pieces.push(
      text.slice(offset, index),
      <mark
        key={index}
        className="rounded bg-crimson/10 text-crimson-dark dark:bg-crimson/30 dark:text-rose-300"
      >
        {match[0]}
      </mark>,
    );
    offset = index + match[0].length;
  }
  pieces.push(text.slice(offset));
  return <>{pieces}</>;
}
export type SidebarReorderProps = HTMLAttributes<HTMLDivElement> & {
  'data-drop-position'?: 'before' | 'after';
};
function DropIndicator({ position }: { position?: 'before' | 'after' }) {
  return position ? (
    <span
      aria-hidden="true"
      className={`pointer-events-none absolute inset-x-0 z-10 h-px bg-crimson dark:bg-rose-400 ${position === 'after' ? 'bottom-0' : 'top-0'}`}
    />
  ) : null;
}
/** Mouse actions reveal on hover; touch actions stay visible and easy to reach. */
const ROW_ACTION =
  'ui-focus-ring absolute top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-300/50 hover:text-gray-800 focus:opacity-100 disabled:cursor-wait disabled:opacity-60 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100 max-md:h-11 max-md:w-11 max-md:opacity-100 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 [@media(pointer:coarse)]:opacity-100';
export interface RowActions {
  activeKey: string | null;
  isUnread: (row: SidebarConversation) => boolean;
  onRead: (row: SidebarConversation) => void;
  archiving: string | null;
  pinning: string | null;
  onArchive: (key: string, jobId: string, jobIds: string[]) => void;
  onSetPinned: (key: string, jobId: string, repo: string, pinned: boolean) => void;
}
export function TaskRow({
  row,
  flat = false,
  query = '',
  search = false,
  reorderProps,
  activeKey,
  isUnread,
  onRead,
  archiving,
  pinning,
  onArchive,
  onSetPinned,
}: RowActions & {
  row: SidebarConversation;
  flat?: boolean;
  query?: string;
  search?: boolean;
  reorderProps?: SidebarReorderProps;
}) {
  const t = useT();
  const { key, job, jobIds } = row;
  const isActive = key === activeKey;
  const conversationState = getConversationState(job);
  const inProgress = conversationState === 'running' || conversationState === 'queued';
  const stateLabel = t(CONVERSATION_STATE_KEYS[conversationState]);
  const review = useSessionReview(job);
  useEffect(() => {
    if (isActive && !inProgress) onRead(row);
  }, [isActive, inProgress, row, onRead]);
  // When the conversation last moved — the one thing that tells a dozen rows
  // with the same truncated title apart. A pinned row already has its pin
  // sitting in this corner, so it goes without.
  const activity = job.pinnedAt ? null : activityLabel(row.lastActivityAt, Date.now(), t);
  return (
    <div className="group relative" {...reorderProps}>
      <DropIndicator position={reorderProps?.['data-drop-position']} />
      <Link
        href={`/${job.id}`}
        onClick={() => onRead(row)}
        aria-label={
          flat
            ? `${job.title} · ${job.repo}${row.archived ? ` · ${t('sidebar.archived')}` : ''}`
            : job.title
        }
        aria-current={isActive ? 'page' : undefined}
        data-search-result={search ? 'true' : undefined}
        title={`${job.repo} · ${job.title}`}
        className={`ui-focus-ring flex min-h-9 w-full items-center gap-2 rounded-lg py-1.5 pl-2 pr-20 text-left text-[13px] transition-colors max-md:min-h-12 max-md:pr-24 [@media(pointer:coarse)]:min-h-12 [@media(pointer:coarse)]:pr-24 ${
          isActive
            ? 'bg-gray-200/70 font-medium text-gray-900 dark:bg-white/10 dark:text-gray-50'
            : 'text-gray-600 hover:bg-gray-200/50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100'
        }`}
      >
        <span className="flex h-3 w-3 shrink-0 items-center justify-center">
          {review ? (
            <span
              aria-hidden="true"
              className="flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-100 text-[10px] font-bold text-amber-800 dark:bg-amber-900/60 dark:text-amber-300"
            >
              !
            </span>
          ) : (
            <span
              aria-label={stateLabel}
              title={stateLabel}
              className={
                conversationState === 'running'
                  ? 'h-3 w-3 animate-spin rounded-full border-[1.5px] border-gray-400 border-t-transparent dark:border-gray-500 dark:border-t-transparent motion-reduce:animate-none'
                  : conversationState === 'queued'
                    ? 'h-1.5 w-1.5 rounded-full bg-gray-400 dark:bg-gray-500'
                    : conversationState === 'failed'
                      ? 'h-1.5 w-1.5 rounded-full bg-red-500'
                      : 'h-1.5 w-1.5 rounded-full border border-gray-400 dark:border-gray-500'
              }
            />
          )}
        </span>
        <span className="min-w-0 flex-1 truncate">
          <Highlight text={job.title} query={query} />
          <SessionReviewBadge review={review} className="block truncate text-[11px]" />
          {flat && (
            <span className="block truncate text-[11px] font-normal text-gray-500 dark:text-gray-400">
              <Highlight text={job.repo} query={query} />
              {row.archived ? ` · ${t('sidebar.archived')}` : ''}
            </span>
          )}
        </span>
        {!inProgress && !isActive && isUnread(row) ? (
          <span
            aria-label={t('sidebar.unread')}
            title={t('sidebar.unread')}
            className="h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500"
          />
        ) : null}
      </Link>
      {activity && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[11px] tabular-nums text-gray-400 transition-opacity group-hover:opacity-0 group-focus-within:opacity-0 dark:text-gray-500 max-md:hidden [@media(pointer:coarse)]:hidden"
        >
          {activity}
        </span>
      )}
      <button
        type="button"
        aria-label={
          job.pinnedAt
            ? t('sidebar.unpin', { title: job.title })
            : t('sidebar.pin', { title: job.title })
        }
        aria-pressed={Boolean(job.pinnedAt)}
        aria-busy={pinning === key}
        title={job.pinnedAt ? t('sidebar.unpinTask') : t('sidebar.pinTask')}
        disabled={pinning === key || archiving === key}
        onClick={() => onSetPinned(key, job.id, job.repo, !job.pinnedAt)}
        className={`${ROW_ACTION} right-10 max-md:right-12 [@media(pointer:coarse)]:right-12 ${
          job.pinnedAt
            ? 'opacity-100'
            : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
        } ${pinning === key ? 'animate-pulse' : ''}`}
      >
        <svg
          className="h-3.5 w-3.5"
          viewBox="0 0 24 24"
          fill={job.pinnedAt ? 'currentColor' : 'none'}
          stroke="currentColor"
          strokeWidth={1.8}
          aria-hidden="true"
        >
          <path
            d="m15 4 5 5-3.5 1.5-4 4L13 19l-1 1-3.5-4.5-4.5-3.5 1-1 4.5.5 4-4L15 4ZM5 19l4-4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      <button
        type="button"
        aria-label={t('sidebar.archive', { title: job.title })}
        title={t('sidebar.archiveTask')}
        disabled={row.archived || archiving === key || pinning === key}
        onClick={() => onArchive(key, job.id, jobIds)}
        className={`${ROW_ACTION} right-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100`}
      >
        <svg
          className="h-4 w-4"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.8}
          aria-hidden="true"
        >
          <path
            d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </div>
  );
}

export function ListStatus({
  loading,
  error,
  empty,
  more,
  onMore,
  onRetry,
  search = false,
}: {
  loading: boolean;
  error: string | null;
  empty: boolean;
  more: boolean;
  onMore: () => void;
  onRetry: () => void;
  search?: boolean;
}) {
  const t = useT();
  return (
    <>
      {loading && (
        <p role="status" className="px-2 py-1.5 text-[12px] text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </p>
      )}
      {error && (
        <p role="alert" className="px-2 py-1.5 text-[12px] text-red-500">
          {error}{' '}
          <button onClick={onRetry} className="underline">
            {t('sidebar.retry')}
          </button>
        </p>
      )}
      {!loading && !error && empty && (
        <p className="px-2 py-1.5 text-[12px] text-gray-500 dark:text-gray-400">
          {t(search ? 'sidebar.noMatches' : 'sidebar.noJobs')}
        </p>
      )}
      {more && (
        <button
          disabled={loading}
          onClick={onMore}
          className="ui-focus-ring min-h-9 w-full rounded-lg px-2 py-1.5 text-left text-[12px] text-gray-500 transition-colors hover:bg-gray-200/50 hover:text-gray-800 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-200 [@media(pointer:coarse)]:min-h-11"
        >
          {t('sidebar.loadMore')}
        </button>
      )}
    </>
  );
}
export interface SidebarPinUpdate {
  row: SidebarConversation;
  refresh: number;
}
/** Keep a completed pin action visible until this particular list catches up. */
export function reconcileSidebarPins(
  rows: SidebarConversation[],
  updates: ReadonlyMap<string, SidebarPinUpdate>,
  loadedRefresh: number | null,
  includeMoved = false,
): SidebarConversation[] {
  const byKey = new Map(rows.map((row) => [row.key, row]));
  for (const [key, update] of updates) {
    if (loadedRefresh !== null && loadedRefresh >= update.refresh) continue;
    const row = byKey.get(key);
    if (row) byKey.set(key, { ...row, job: { ...row.job, pinnedAt: update.row.job.pinnedAt } });
    else if (includeMoved) byKey.set(key, update.row);
  }
  return [...byKey.values()];
}

export function ProjectFolder({
  repo,
  active,
  activeCount,
  expanded,
  onToggle,
  initial,
  selected,
  userId,
  refresh,
  hiddenThreads,
  pinUpdates,
  registerRows,
  orderRows,
  taskReorder,
  reorderProps,
  ...actions
}: RowActions & {
  repo: string;
  active: boolean;
  activeCount: number;
  expanded: boolean;
  onToggle: () => void;
  initial: SidebarConversation[];
  selected?: SidebarConversation;
  userId?: string;
  refresh: number;
  hiddenThreads: Set<string>;
  pinUpdates?: ReadonlyMap<string, SidebarPinUpdate>;
  registerRows: (rows: SidebarConversation[], loadedRefresh: number | null) => void;
  orderRows: (rows: SidebarConversation[]) => SidebarConversation[];
  taskReorder: (rows: SidebarConversation[], row: SidebarConversation) => SidebarReorderProps;
  reorderProps: SidebarReorderProps;
}) {
  const t = useT();
  const list = useConversationList({ repo, pinned: false, enabled: expanded, userId, refresh });
  const [shown, setShown] = useState(15);
  useEffect(() => {
    registerRows(list.rows, list.loadedRefresh);
  }, [list.rows, list.loadedRefresh, registerRows]);
  const byKey = new Map(initial.map((row) => [row.key, row]));
  for (const row of list.rows) byKey.set(row.key, row);
  if (selected && !selected.archived && !byKey.has(selected.key)) byKey.set(selected.key, selected);
  const rows = orderRows(
    reconcileSidebarPins([...byKey.values()], pinUpdates ?? new Map(), list.loadedRefresh, true)
      .filter(
        (row) =>
          row.job.repo === repo &&
          !row.job.pinnedAt &&
          !row.archived &&
          !hiddenThreads.has(row.key),
      )
      .sort(
        (a, b) =>
          (Date.parse(b.lastActivityAt ?? '') || 0) - (Date.parse(a.lastActivityAt ?? '') || 0),
      ),
  );
  const selectedIndex = rows.findIndex((row) => row.key === actions.activeKey);
  const visible = rows.slice(0, Math.max(shown, selectedIndex + 1));
  const listId = `project-${encodeURIComponent(repo)}`;
  return (
    <div className="mt-1">
      {/* A folder is a heading for the rows under it, not a destination, so it
          is never painted as selected: the selected row is inside it. The one
          it contains is told apart by weight and a crimson folder icon, which
          still reads when the folder is collapsed. */}
      <div
        className="group relative flex items-center gap-0.5 rounded-md transition-colors hover:bg-gray-200/50 dark:hover:bg-white/5"
        {...reorderProps}
      >
        <DropIndicator position={reorderProps['data-drop-position']} />
        <button
          type="button"
          data-reorder-project
          draggable={Boolean(reorderProps.draggable)}
          aria-expanded={expanded}
          aria-controls={listId}
          aria-current={active ? 'true' : undefined}
          title={repo}
          onClick={onToggle}
          className={`ui-focus-ring flex min-h-9 min-w-0 flex-1 items-center gap-1.5 rounded-lg py-1.5 pl-1.5 pr-1 text-left text-[13px] font-medium max-md:min-h-12 [@media(pointer:coarse)]:min-h-12 ${active ? 'text-gray-900 dark:text-gray-50' : 'text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100'}`}
        >
          <svg
            className={`h-3 w-3 shrink-0 text-gray-400 transition-transform dark:text-gray-500 ${expanded ? 'rotate-90' : ''}`}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden="true"
          >
            <path d="m9 6 6 6-6 6" />
          </svg>
          <svg
            className={`h-4 w-4 shrink-0 ${active ? 'text-crimson dark:text-crimson-light' : 'text-gray-400 dark:text-gray-500'}`}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            aria-hidden="true"
          >
            <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17V7.5Z" />
          </svg>
          <span className="min-w-0 flex-1 truncate">{projectLabel(repo)}</span>
          {!expanded && activeCount > 0 && (
            <span
              aria-label={t('sidebar.runningOrQueued', { count: activeCount })}
              className="h-3 w-3 shrink-0 animate-spin rounded-full border-[1.5px] border-gray-400 border-t-transparent dark:border-gray-500 dark:border-t-transparent motion-reduce:animate-none"
            />
          )}
        </button>
        <Link
          href={`/?repo=${encodeURIComponent(repo)}`}
          draggable={reorderProps.draggable ? false : undefined}
          data-row-action
          aria-label={t('sidebar.newTaskIn', { project: projectLabel(repo) })}
          title={t('sidebar.newTaskIn', { project: projectLabel(repo) })}
          className="ui-focus-ring mr-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-gray-500 opacity-0 transition-colors hover:bg-gray-300/50 hover:text-gray-800 focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100 max-md:h-11 max-md:w-11 max-md:opacity-100 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 [@media(pointer:coarse)]:opacity-100"
        >
          <svg
            className="h-3.5 w-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
        </Link>
      </div>
      {expanded && (
        <div
          id={listId}
          className="ml-[13px] mt-0.5 space-y-0.5 border-l border-gray-200 pl-1.5 dark:border-white/10"
        >
          {visible.map((row) => (
            <TaskRow key={row.key} row={row} {...actions} reorderProps={taskReorder(rows, row)} />
          ))}
          <ListStatus
            loading={list.showLoading}
            error={list.error}
            empty={!rows.length}
            more={rows.length > visible.length || Boolean(list.cursor)}
            onMore={() => {
              setShown((v) => v + 15);
              if (rows.length <= visible.length + 15) list.loadMore();
            }}
            onRetry={list.reload}
          />
        </div>
      )}
    </div>
  );
}
