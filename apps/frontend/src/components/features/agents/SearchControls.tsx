import type { KeyboardEventHandler, Ref } from 'react';
import { useT } from '@/components/providers';
import { projectTitle } from './conversations';

export function SearchControls({
  inputRef,
  query,
  onQueryChange,
  scope,
  onScopeChange,
  activeRepo,
  includeArchived,
  onIncludeArchivedChange,
  onClose,
  onInputKeyDown,
}: {
  inputRef: Ref<HTMLInputElement>;
  query: string;
  onQueryChange: (value: string) => void;
  scope: string;
  onScopeChange: (value: 'all' | 'current') => void;
  activeRepo: string | null;
  includeArchived: boolean;
  onIncludeArchivedChange: (value: boolean) => void;
  onClose: () => void;
  onInputKeyDown: KeyboardEventHandler<HTMLInputElement>;
}) {
  const t = useT();
  const currentOnly = scope === 'current';
  const chipClass = (selected: boolean) =>
    `inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 disabled:cursor-default ${
      selected
        ? 'bg-crimson/[0.08] text-crimson-dark hover:bg-crimson/15 dark:bg-crimson/20 dark:text-rose-300 dark:hover:bg-crimson/30'
        : 'text-gray-500 hover:bg-gray-200/60 hover:text-gray-700 disabled:hover:bg-transparent disabled:hover:text-gray-500 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200 dark:disabled:hover:text-gray-400'
    }`;
  return (
    <div
      className="mb-3 space-y-1.5 px-1"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="flex min-w-0 items-center gap-1 rounded-lg border border-gray-200 bg-white pl-2.5 pr-1 transition-colors focus-within:border-crimson/40 focus-within:ring-2 focus-within:ring-crimson/[0.06] dark:border-white/10 dark:bg-gray-800 dark:focus-within:border-crimson/60">
        <svg
          className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          aria-hidden="true"
        >
          <circle cx="10" cy="10" r="6.5" />
          <path d="m15 15 5 5" />
        </svg>
        <input
          ref={inputRef}
          value={query}
          aria-label={t('sidebar.search')}
          placeholder={t('sidebar.searchPlaceholder')}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={onInputKeyDown}
          className="min-w-0 flex-1 bg-transparent px-1 py-2 text-[13px] text-gray-800 outline-none placeholder:text-gray-400 dark:text-gray-100 dark:placeholder:text-gray-500"
        />
        <button
          type="button"
          onClick={onClose}
          aria-label={t('sidebar.closeSearch')}
          title={`${t('sidebar.closeSearch')} (Esc)`}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
        >
          <svg
            className="h-3.5 w-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            aria-hidden="true"
          >
            <path d="m7 7 10 10M17 7 7 17" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5">
        <button
          type="button"
          aria-label={t('sidebar.searchScope')}
          aria-pressed={currentOnly}
          disabled={!activeRepo}
          title={
            !activeRepo
              ? t('sidebar.searchScopeUnavailable')
              : currentOnly
                ? t('sidebar.searchAllProjects')
                : t('sidebar.searchCurrentProject', { repo: projectTitle(activeRepo, t) })
          }
          onClick={() => onScopeChange(currentOnly ? 'all' : 'current')}
          className={chipClass(currentOnly)}
        >
          <svg
            className="h-3.5 w-3.5 shrink-0"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            aria-hidden="true"
          >
            <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v7a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17V7.5Z" />
          </svg>
          <span className="truncate">
            {t(currentOnly ? 'sidebar.currentProject' : 'sidebar.allProjects')}
          </span>
        </button>
        <button
          type="button"
          aria-label={t('sidebar.includeArchived')}
          aria-pressed={includeArchived}
          title={t('sidebar.includeArchived')}
          onClick={() => onIncludeArchivedChange(!includeArchived)}
          className={chipClass(includeArchived)}
        >
          <svg
            className="h-3.5 w-3.5 shrink-0"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            aria-hidden="true"
          >
            <path
              d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="truncate">{t('sidebar.includeArchived')}</span>
        </button>
      </div>
    </div>
  );
}
