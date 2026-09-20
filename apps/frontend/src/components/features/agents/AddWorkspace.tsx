import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';

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
  const [position, setPosition] = useState<{ userId: string; top: number; left: number } | null>(
    null,
  );
  const [query, setQuery] = useState('');
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<{
    userId?: string;
    config?: AgentConfigApi;
    error?: string;
  }>({});
  const open = Boolean(position && position.userId === userId);
  const config = data.userId === userId ? data.config : undefined;
  const error = data.userId === userId ? data.error : undefined;
  const close = () => {
    setPosition(null);
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
        setPosition(null);
    };
    const reposition = () => setPosition(null);
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', reposition);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', reposition);
    };
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
        className="rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-200/60 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
        onClick={() => {
          if (open) return close();
          if (!userId) return;
          const rect = trigger.current!.getBoundingClientRect();
          setQuery('');
          setData({ userId });
          setPosition({
            userId,
            left: Math.max(8, Math.min(rect.right - 288, window.innerWidth - 296)),
            top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 340)),
          });
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
        position &&
        createPortal(
          <div
            ref={panel}
            role="dialog"
            aria-label={t('sidebar.addWorkspace')}
            style={{ top: position.top, left: position.left }}
            className="fixed z-50 w-72 max-w-[calc(100vw-16px)] rounded-xl border border-gray-200 bg-white p-2 text-[13px] shadow-xl dark:border-white/10 dark:bg-gray-800"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close();
              }
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
            <div className="mb-2 flex items-center justify-between px-1">
              <span className="font-medium text-gray-800 dark:text-gray-100">
                {t('sidebar.addWorkspace')}
              </span>
              <button
                type="button"
                onClick={close}
                aria-label={t('sidebar.closeWorkspacePicker')}
                className="rounded px-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/10"
              >
                ×
              </button>
            </div>
            <input
              ref={input}
              value={query}
              aria-label={t('sidebar.searchRepositories')}
              placeholder={t('sidebar.searchRepositories')}
              onChange={(event) => setQuery(event.target.value)}
              className="mb-2 w-full rounded-lg border border-gray-200 bg-transparent px-2.5 py-2 outline-none focus:border-crimson dark:border-white/10"
            />
            <div className="max-h-52 overflow-y-auto">
              {!config && error === undefined && (
                <p role="status" className="px-2 py-3 text-gray-500 dark:text-gray-400">
                  {t('common.loading')}
                </p>
              )}
              {error !== undefined && (
                <p role="alert" className="px-2 py-3 text-red-500">
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
                    className="flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-gray-800 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-100 dark:hover:bg-white/10 dark:focus:bg-white/10"
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
                <p className="px-2 py-3 text-gray-500 dark:text-gray-400">
                  {t(!config.github_connected ? 'composer.connectToRun' : 'sidebar.noRepositories')}
                </p>
              )}
            </div>
            {config && (
              <Link
                href="/integrations"
                onClick={close}
                className="mt-1 block border-t border-gray-100 px-2.5 py-2 text-crimson-dark dark:border-white/10 dark:text-rose-300"
              >
                {t('sidebar.manageRepositories')}
              </Link>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
