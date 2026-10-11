'use client';

import Link from 'next/link';
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { useT } from '@/components/providers';
import { COMPOSER_MENU_ROW, COMPOSER_MENU_SURFACE } from './composerMenuStyles';
import { useComposerMenuLayout } from './useComposerMenuLayout';

// Which repository a new task works on — chosen, never assumed.
//
// This was a native select that started on the first repository in the list,
// so a task could be sent without its owner ever looking at where it would
// run. One user did not realise their first message would change a GitHub
// repository at all. A task edits real code and opens a real pull request, so
// the control starts empty and the composer will not run until it has a value.
//
// Searchable, because the list is every repository the owner's GitHub App
// installations cover, and one organization can bring a hundred. The footer is
// where a missing repository is fixed: GitHub decides what the App can see,
// and Refresh reads GitHub's answer again.

const CONFIGURE_CLASS =
  'flex min-w-0 flex-1 items-center gap-2 rounded-lg px-3 py-2 text-left text-[13px] text-gray-700 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-200 dark:hover:bg-white/10 dark:focus:bg-white/10';

/** `owner/` and `name`, so the owner can be drawn quieter than the name. */
function splitRepository(repo: string): [string, string] {
  const slash = repo.indexOf('/');
  return slash < 0 ? ['', repo] : [repo.slice(0, slash + 1), repo.slice(slash + 1)];
}

function GitHubMark({ className }: { className: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

export function RepositoryPicker({
  repos,
  value,
  onChange,
  open,
  onOpenChange,
  onRefresh,
  refreshing = false,
  onConfigure,
  onCreate,
  createDisabled = false,
  returnFocusTo,
}: {
  repos: string[];
  value: string;
  onChange: (repo: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Read the repository list again; absent means the footer offers no refresh. */
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Go straight to GitHub to add repositories; without it, the Integrations page. */
  onConfigure?: () => void;
  /** Create a personal GitHub repository before starting a conversation. */
  onCreate?: () => void;
  /** Keep asynchronous composer work from moving focus behind the creation dialog. */
  createDisabled?: boolean;
  /** Where focus goes after a choice. The trigger, unless the composer asked. */
  returnFocusTo?: RefObject<HTMLElement>;
}) {
  const t = useT();
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const menuStyle = useComposerMenuLayout(open, panel);
  // Read through a ref so a caller's inline handler cannot re-run the effect
  // below on every render, which would refocus the search box mid-typing.
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  // Case-insensitive, so `alice/…` does not sort after every `Zeta-Org/…`.
  const sorted = useMemo(
    () =>
      [...new Set(repos)].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' })),
    [repos],
  );
  const needle = query.trim().toLocaleLowerCase();
  const matches = needle
    ? sorted.filter((repo) => repo.toLocaleLowerCase().includes(needle))
    : sorted;

  // The open menu must own the caret as soon as it appears, including when a
  // restored draft opens it during an asynchronous config update.
  useLayoutEffect(() => {
    if (open) search.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (panel.current?.contains(target) || trigger.current?.contains(target)) return;
      onOpenChangeRef.current(false);
    }
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      setQuery('');
    };
  }, [open]);

  function close() {
    onOpenChange(false);
    trigger.current?.focus();
  }

  function choose(repo: string) {
    onChange(repo);
    onOpenChange(false);
    (returnFocusTo?.current ?? trigger.current)?.focus();
  }

  function moveFocus(key: string) {
    const options = Array.from(
      panel.current?.querySelectorAll<HTMLButtonElement>('[data-repository-option]') ?? [],
    );
    if (!options.length) return;
    const at = options.indexOf(document.activeElement as HTMLButtonElement);
    if (key === 'ArrowUp' && at <= 0) {
      search.current?.focus();
      return;
    }
    const next =
      key === 'Home'
        ? 0
        : key === 'End'
          ? options.length - 1
          : Math.min(options.length - 1, at + (key === 'ArrowDown' ? 1 : -1));
    options[next]?.focus();
  }

  const [owner, name] = splitRepository(value);
  const configure = (
    <>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        aria-hidden="true"
        className="h-4 w-4 shrink-0"
      >
        <path strokeLinecap="round" d="M12 5v14M5 12h14" />
      </svg>
      <span className="truncate">{t('composer.repositoryConfigure')}</span>
    </>
  );

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        // Named for the control and its value together, so a screen reader
        // hears which repository — or that none is chosen yet.
        aria-label={`${t('composer.repository')}: ${value || t('composer.repositoryPlaceholder')}`}
        title={value || t('composer.repositoryPlaceholder')}
        onClick={() => onOpenChange(!open)}
        className={`ui-focus-ring inline-flex min-h-10 min-w-0 max-w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11 ${
          open ? 'bg-surface-hover' : ''
        } ${value ? 'text-foreground' : 'text-accent'}`}
      >
        <GitHubMark className="h-3.5 w-3.5 shrink-0" />
        <span className="max-w-[10rem] truncate sm:max-w-[13rem]">
          {value ? (
            <>
              <span className="text-muted">{owner}</span>
              {name}
            </>
          ) : (
            t('composer.repositoryPlaceholder')
          )}
        </span>
        <svg
          aria-hidden
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          className="h-3 w-3 shrink-0 text-muted"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 8 4 4 4-4" />
        </svg>
      </button>
      {open ? (
        <div
          ref={panel}
          id={id}
          role="dialog"
          aria-label={t('composer.repository')}
          className={`${COMPOSER_MENU_SURFACE} flex flex-col`}
          style={menuStyle}
          onBlur={(event) => {
            if (
              event.relatedTarget &&
              !event.currentTarget.contains(event.relatedTarget as Node) &&
              !trigger.current?.contains(event.relatedTarget as Node)
            )
              onOpenChange(false);
          }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
              close();
            } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
              // Home and End belong to the search box while it has the caret.
              if (event.target === search.current && ['Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              moveFocus(event.key);
            }
          }}
        >
          <input
            ref={search}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || event.nativeEvent.isComposing) return;
              // Enter takes the first match, so typing part of a name is enough.
              event.preventDefault();
              if (matches[0]) choose(matches[0]);
            }}
            aria-label={t('composer.repositorySearch')}
            placeholder={t('composer.repositorySearch')}
            className="mb-1 w-full shrink-0 rounded-lg bg-transparent px-3 py-2 text-[13px] text-gray-900 placeholder:text-gray-400 focus:outline-none dark:text-gray-100 dark:placeholder:text-gray-500"
          />
          <div className="min-h-0 flex-1 overflow-y-auto border-t border-gray-100 pt-1 dark:border-white/5">
            {matches.map((repo) => {
              const [repoOwner, repoName] = splitRepository(repo);
              return (
                <button
                  key={repo}
                  type="button"
                  data-repository-option
                  aria-label={repo}
                  aria-current={repo === value ? 'true' : undefined}
                  title={repo}
                  onClick={() => choose(repo)}
                  className={COMPOSER_MENU_ROW}
                >
                  <GitHubMark className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="text-gray-500 dark:text-gray-400">{repoOwner}</span>
                    {repoName}
                  </span>
                  {repo === value ? (
                    <svg
                      aria-hidden
                      viewBox="0 0 20 20"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={1.8}
                      className="h-4 w-4 shrink-0 text-gray-500 dark:text-gray-400"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="m5 10.5 3.2 3L15 6.5" />
                    </svg>
                  ) : null}
                </button>
              );
            })}
            {matches.length === 0 ? (
              <p className="px-3 py-3 text-[13px] leading-relaxed text-gray-500 dark:text-gray-400">
                {sorted.length === 0
                  ? t('composer.repositoryEmpty')
                  : t('composer.repositoryNoMatch', { query: query.trim() })}{' '}
                {t('composer.repositoryMissing')}
              </p>
            ) : null}
          </div>
          {onCreate ? (
            <button
              type="button"
              disabled={createDisabled}
              onClick={() => {
                if (createDisabled) return;
                close();
                onCreate();
              }}
              className={`${COMPOSER_MENU_ROW} mt-1 shrink-0 border-t border-gray-100 pt-2 font-medium disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/5`}
            >
              <span aria-hidden="true" className="w-4 text-center">
                +
              </span>
              {t('repositoryCreate.title')}
            </button>
          ) : null}
          <div className="mt-1 flex shrink-0 items-center gap-1 border-t border-gray-100 pt-1 dark:border-white/5">
            <a
              href="https://github.com/new"
              target="_blank"
              rel="noreferrer"
              className={CONFIGURE_CLASS}
            >
              <GitHubMark className="h-4 w-4 shrink-0" />
              <span className="truncate">{t('composer.repositoryCreate')}</span>
            </a>
            {onConfigure ? (
              <button
                type="button"
                onClick={() => {
                  onOpenChange(false);
                  onConfigure();
                }}
                className={CONFIGURE_CLASS}
              >
                {configure}
              </button>
            ) : (
              <Link href="/integrations" className={CONFIGURE_CLASS}>
                {configure}
              </Link>
            )}
            {onRefresh ? (
              <button
                type="button"
                onClick={onRefresh}
                disabled={refreshing}
                className="shrink-0 rounded-lg px-3 py-2 text-[13px] text-gray-500 hover:bg-gray-100 hover:text-gray-800 focus:bg-gray-100 focus:outline-none disabled:cursor-wait disabled:opacity-60 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100 dark:focus:bg-white/10"
              >
                {refreshing ? t('composer.repositoryRefreshing') : t('composer.repositoryRefresh')}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
