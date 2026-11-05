import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import {
  MENU_EMPTY,
  menuRowClass,
  MENU_FOOTER_ACTION_ACCENT,
  MENU_HEADING,
  MENU_POSITIONED,
  MENU_ROW,
  MENU_SCROLL,
  MENU_SEARCH,
  MENU_SEARCH_INPUT,
  MENU_SURFACE,
} from '@/components/ui/menuStyles';
import { useAnchoredPopover, popoverContainer } from '@/components/ui/anchoredPopover';
import { useTopLayerEscape } from '@/components/ui/useTopLayerEscape';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { DeferredCreateRepositoryDialog } from './DeferredCreateRepositoryDialog';

/** Add an accessible repository to this account's sidebar without creating a task. */
export function AddWorkspace({
  userId,
  existing,
  onSelect,
}: {
  userId?: string;
  existing: string[];
  onSelect: (repo: string) => void;
}) {
  const t = useT();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [openFor, setOpenFor] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [retry, setRetry] = useState(0);
  const [creatingFor, setCreatingFor] = useState<string | null>(null);
  const [data, setData] = useState<{
    userId?: string;
    config?: AgentConfigApi;
    error?: string;
  }>({});
  const open = Boolean(openFor && openFor === userId);
  const style = useAnchoredPopover(open, trigger, panel, {
    gap: 6,
    width: 288,
    maxWidth: 320,
    preferredHeight: 300,
    maxHeight: 360,
  });
  const [container, setContainer] = useState<HTMLElement | null>(null);
  useTopLayerEscape(open, () => close(), trigger);
  const config = data.userId === userId ? data.config : undefined;
  const error = data.userId === userId ? data.error : undefined;
  const close = () => {
    setOpenFor(null);
    trigger.current?.focus();
  };
  useEffect(() => {
    if (!open || !userId) return;
    let cancelled = false;
    setData({ userId });
    getAgentConfig()
      .then((config) => {
        if (!cancelled) setData({ userId, config });
      })
      .catch((cause: unknown) => {
        if (!cancelled) setData({ userId, error: cause instanceof Error ? cause.message : '' });
      });
    return () => {
      cancelled = true;
    };
  }, [open, userId, retry]);
  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const outside = (event: PointerEvent) => {
      if (
        !panel.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setOpenFor(null);
    };
    // Escape is `useTopLayerEscape`'s, so a menu opened above this panel
    // closes first instead of this panel taking the key.
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const matches = [...new Set(config?.repos ?? [])].filter((repo) =>
    repo.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );
  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label={t('sidebar.addWorkspace')}
        title={t('sidebar.addWorkspace')}
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={!userId}
        className="ui-focus-ring rounded-md p-1 text-muted transition-colors hover:bg-surface-hover hover:text-foreground disabled:opacity-50"
        onClick={() => {
          if (open) return close();
          if (!userId) return;
          setQuery('');
          setData({ userId });
          setContainer(popoverContainer(trigger.current));
          setOpenFor(userId);
        }}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          aria-hidden="true"
        >
          <path
            d="M14 6h-4L8 3H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7M19 2v8m-4-4h8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open &&
        createPortal(
          <div
            ref={panel}
            role="dialog"
            aria-label={t('sidebar.addWorkspace')}
            style={style}
            className={`${MENU_SURFACE} ${MENU_POSITIONED} w-72 max-w-[calc(100vw-16px)]`}
            onKeyDown={(event) => {
              if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
              event.preventDefault();
              const choices = [
                ...panel.current!.querySelectorAll<HTMLButtonElement>('[data-workspace-option]'),
              ];
              const index = choices.indexOf(document.activeElement as HTMLButtonElement);
              const next = choices[index + (event.key === 'ArrowDown' ? 1 : -1)];
              if (next) next.focus();
              else if (event.key === 'ArrowUp') input.current?.focus();
            }}
          >
            <div className="flex items-center justify-between gap-2 px-1">
              <span className={MENU_HEADING}>{t('sidebar.addWorkspace')}</span>
              <button
                type="button"
                onClick={close}
                aria-label={t('sidebar.closeWorkspacePicker')}
                className="ui-focus-ring rounded px-1.5 text-muted hover:bg-surface-hover hover:text-foreground"
              >
                ×
              </button>
            </div>
            <div className={MENU_SEARCH}>
              <input
                ref={input}
                value={query}
                aria-label={t('sidebar.searchRepositories')}
                placeholder={t('sidebar.searchRepositories')}
                onChange={(event) => setQuery(event.target.value)}
                className={MENU_SEARCH_INPUT}
              />
            </div>
            <div className={`${MENU_SCROLL} border-t border-border pt-1`}>
              {!config && error === undefined && (
                <p role="status" className={MENU_EMPTY}>
                  {t('common.loading')}
                </p>
              )}
              {error !== undefined && (
                <p role="alert" className={`${MENU_EMPTY} text-status-error`}>
                  {error || t('composer.loadConfigFailed')}{' '}
                  <button
                    type="button"
                    className="underline"
                    onClick={() => setRetry((v) => v + 1)}
                  >
                    {t('sidebar.retry')}
                  </button>
                </p>
              )}
              {config?.github_connected &&
                matches.map((repo) => (
                  <button
                    key={repo}
                    type="button"
                    data-workspace-option
                    title={repo}
                    aria-label={
                      existing.includes(repo) ? `${repo} · ${t('sidebar.workspaceAdded')}` : repo
                    }
                    className={MENU_ROW}
                    onClick={() => {
                      onSelect(repo);
                      close();
                    }}
                  >
                    <span className="truncate">{repo}</span>
                    {existing.includes(repo) && <span aria-hidden="true">✓</span>}
                  </button>
                ))}
              {config && (!config.github_connected || !matches.length) && (
                <p className={`${MENU_EMPTY} !py-3`}>
                  {t(
                    !config.github_connected
                      ? 'temporaryWorkspace.connect'
                      : 'sidebar.noRepositories',
                  )}
                </p>
              )}
            </div>
            {config && (
              <Link
                href="/settings/integrations"
                onClick={close}
                className={`${MENU_FOOTER_ACTION_ACCENT} border-t border-border`}
              >
                {t('sidebar.manageRepositories')}
              </Link>
            )}
            {config?.github_connected && (
              <button
                type="button"
                onClick={() => {
                  close();
                  setCreatingFor(userId!);
                }}
                className={`${menuRowClass({ accent: true })} font-medium`}
              >
                {t('repositoryCreate.title')}
              </button>
            )}
          </div>,
          container ?? document.body,
        )}
      <DeferredCreateRepositoryDialog
        key={userId}
        open={Boolean(userId && creatingFor === userId)}
        onClose={() => {
          setCreatingFor(null);
          trigger.current?.focus();
        }}
        onAvailable={(repo, config) => {
          setData({ userId, config });
          onSelect(repo);
        }}
      />
    </>
  );
}
