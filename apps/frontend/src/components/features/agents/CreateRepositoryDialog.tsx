'use client';

import { useModalDialog } from '@/components/ui/useModalDialog';

import Link from 'next/link';
import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import {
  createGitHubRepository,
  getAgentConfig,
  listRepoBranches,
  type AgentConfigApi,
  type CreatedGitHubRepository,
} from '@/lib/api/agents';

const FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm text-gray-900 focus:border-crimson focus:outline-none disabled:opacity-60 dark:border-white/10 dark:text-gray-100';
const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5';
const PRIMARY =
  'rounded-lg bg-crimson px-4 py-2 text-sm font-semibold text-white hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-50';

/** Creation and App access are separate steps; an access retry never creates again. */
export function CreateRepositoryDialog({
  open,
  onClose,
  onAvailable,
}: {
  open: boolean;
  onClose: () => void;
  onAvailable: (repo: string, config: AgentConfigApi) => void;
}) {
  const t = useT();
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const submission = useRef(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [isPrivate, setIsPrivate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<CreatedGitHubRepository | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useModalDialog(panel, { open, busy, onClose, initialFocus: 'input' });

  useEffect(() => {
    if (open && created) panel.current?.querySelector<HTMLElement>('a')?.focus();
  }, [open, created]);

  async function checkAvailability(repository: CreatedGitHubRepository) {
    const config = await getAgentConfig({ fresh: true });
    if (!mounted.current) return;
    if (
      config.github_connected &&
      (repository.accessible || config.repos.includes(repository.repo))
    ) {
      // Config may list an operator allowlist; confirm the App can read the new
      // branch. The creation response's access flag can also age while waiting.
      const branches = await listRepoBranches(repository.repo);
      if (!mounted.current) return;
      if (branches.branches.length > 0) {
        onAvailable(repository.repo, {
          ...config,
          repos: [...new Set([...config.repos, repository.repo])],
        });
        onClose();
        setCreated(null);
        setName('');
        setDescription('');
        setIsPrivate(true);
        setChecked(false);
        return;
      }
    }
    setChecked(true);
  }

  async function create() {
    if (submission.current || created || !name.trim()) return;
    submission.current = true;
    setBusy(true);
    setError(null);
    try {
      const repository = await createGitHubRepository({
        name: name.trim(),
        ...(description.trim() ? { description: description.trim() } : {}),
        private: isPrivate,
      });
      if (!mounted.current) return;
      // Commit success before refreshing: a failed read must not offer another POST.
      setCreated(repository);
      try {
        await checkAvailability(repository);
      } catch (cause: unknown) {
        setError(cause instanceof Error ? cause.message : t('repositoryCreate.refreshFailed'));
      }
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('repositoryCreate.failed'));
    } finally {
      submission.current = false;
      setBusy(false);
    }
  }

  async function refresh() {
    if (!created || submission.current) return;
    submission.current = true;
    setBusy(true);
    setError(null);
    setChecked(false);
    try {
      await checkAvailability(created);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('repositoryCreate.refreshFailed'));
    } finally {
      submission.current = false;
      setBusy(false);
    }
  }

  if (!open || typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('repositoryCreate.close')}
        disabled={busy}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-description`}
        aria-busy={busy}
        className="relative max-h-full w-full max-w-lg overflow-y-auto rounded-2xl border border-gray-200 bg-white p-6 shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex items-start justify-between gap-4">
          <h2 id={`${id}-title`} className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {t(created ? 'repositoryCreate.successTitle' : 'repositoryCreate.title')}
          </h2>
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            aria-label={t('repositoryCreate.close')}
            className="rounded-md px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5"
          >
            ×
          </button>
        </div>
        <p id={`${id}-description`} className="mt-2 text-sm text-gray-500 dark:text-gray-400">
          {t(created ? 'repositoryCreate.accessNeeded' : 'repositoryCreate.description')}
        </p>
        {created ? (
          <div className="mt-5">
            <a
              href={created.web_url}
              target="_blank"
              rel="noopener noreferrer"
              className="break-all text-sm font-medium text-crimson-dark underline dark:text-rose-300"
            >
              {created.repo}
            </a>
            {checked ? (
              <p role="status" className="mt-3 text-sm text-gray-500 dark:text-gray-400">
                {t('repositoryCreate.notAvailable')}
              </p>
            ) : null}
            <div className="mt-5 flex flex-wrap items-center gap-2">
              <Link
                href="/settings/integrations"
                target="_blank"
                rel="noopener noreferrer"
                className={BUTTON}
              >
                {t('composer.repositoryConfigure')}
              </Link>
              <button
                type="button"
                onClick={() => void refresh()}
                disabled={busy}
                className={PRIMARY}
              >
                {t(busy ? 'composer.repositoryRefreshing' : 'repositoryCreate.refresh')}
              </button>
            </div>
          </div>
        ) : (
          <form
            className="mt-5 space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              {t('repositoryCreate.name')}
              <input
                name="name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={100}
                pattern="[A-Za-z0-9][A-Za-z0-9_.-]*"
                autoComplete="off"
                disabled={busy}
                className={FIELD}
              />
            </label>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              {t('repositoryCreate.repositoryDescription')}
              <textarea
                name="description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                maxLength={350}
                rows={2}
                disabled={busy}
                className={FIELD}
              />
            </label>
            <fieldset
              disabled={busy}
              className="space-y-2 text-sm text-gray-700 dark:text-gray-200"
            >
              <legend className="mb-2 font-medium">{t('repositoryCreate.visibility')}</legend>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="visibility"
                  checked={isPrivate}
                  onChange={() => setIsPrivate(true)}
                  className="mt-1 accent-crimson"
                />
                <span>
                  {t('repositoryCreate.private')}
                  <span className="block text-xs text-gray-500 dark:text-gray-400">
                    {t('repositoryCreate.privateDescription')}
                  </span>
                </span>
              </label>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  name="visibility"
                  checked={!isPrivate}
                  onChange={() => setIsPrivate(false)}
                  className="mt-1 accent-crimson"
                />
                <span>
                  {t('repositoryCreate.public')}
                  <span className="block text-xs text-gray-500 dark:text-gray-400">
                    {t('repositoryCreate.publicDescription')}
                  </span>
                </span>
              </label>
            </fieldset>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {t('repositoryCreate.readme')}
            </p>
            <div className="flex justify-end gap-2 border-t border-gray-100 pt-4 dark:border-white/5">
              <button type="button" onClick={onClose} disabled={busy} className={BUTTON}>
                {t('repositoryCreate.cancel')}
              </button>
              <button type="submit" disabled={busy || !name.trim()} className={PRIMARY}>
                {t(busy ? 'repositoryCreate.creating' : 'repositoryCreate.title')}
              </button>
            </div>
          </form>
        )}
        {error ? (
          <p role="alert" className="mt-4 text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
