'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  deleteSshServer,
  listSshServers,
  rotateSshServerKey,
  type SshServerApi,
} from '@/lib/api/sshServers';
import { getErrorMessage } from '@/lib/utils/errors';
import { SshCheckDialog } from './SshCheckDialog';
import { SshServerDialog } from './SshServerDialog';

const ACTION = 'ui-button ui-button-secondary min-h-11';
const MENU_ITEM =
  'ui-focus-ring block min-h-11 w-full rounded-lg px-3 py-2 text-left text-sm text-foreground hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-60';

function ServerIcon() {
  return (
    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-border bg-surface-muted text-foreground">
      <svg
        className="h-5 w-5"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        aria-hidden="true"
      >
        <rect x="3" y="4" width="18" height="7" rx="2" />
        <rect x="3" y="13" width="18" height="7" rx="2" />
        <path strokeLinecap="round" d="M7 7.5h.01M7 16.5h.01" />
      </svg>
    </span>
  );
}

/** The owner's SSH servers: add, check, rename, rotate the key, delete. */
export function SshServersSection() {
  const t = useT();
  const [servers, setServers] = useState<SshServerApi[]>([]);
  const [configured, setConfigured] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<SshServerApi | 'new' | null>(null);
  const [checking, setChecking] = useState<SshServerApi | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const latest = useRef(0);

  const load = useCallback(async () => {
    const request = ++latest.current;
    setLoading(true);
    setError(null);
    try {
      const found = await listSshServers();
      if (request !== latest.current) return undefined;
      setServers(found.servers);
      setConfigured(found.configured);
      return found.servers;
    } catch (cause) {
      if (request === latest.current) setError(getErrorMessage(cause) || t('ssh.loadFailed'));
      return undefined;
    } finally {
      if (request === latest.current) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const replace = useCallback((server: SshServerApi) => {
    setServers((old) =>
      old.some((row) => row.id === server.id)
        ? old.map((row) => (row.id === server.id ? server : row))
        : [...old, server],
    );
  }, []);

  async function rotate(server: SshServerApi) {
    setBusy(server.id);
    setError(null);
    setNotice(null);
    try {
      const next = await rotateSshServerKey(server);
      replace(next);
      setChecking(next);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(null);
    }
  }

  async function remove(server: SshServerApi) {
    setBusy(server.id);
    setError(null);
    setNotice(null);
    try {
      await deleteSshServer(server);
      setServers((old) => old.filter((row) => row.id !== server.id));
      setNotice(t('ssh.deleted', { name: server.name }));
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  }

  return (
    <section aria-labelledby="ssh-servers-heading" className="ui-card mt-4 bg-surface px-3 sm:px-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-3">
        <h2 id="ssh-servers-heading" className="text-sm font-semibold text-foreground">
          {t('ssh.title')}
        </h2>
        <button
          type="button"
          disabled={!configured || loading}
          onClick={() => setEditing('new')}
          className="ui-button ui-button-primary min-h-11"
        >
          {t('ssh.add')}
        </button>
      </div>
      <div className="space-y-1 py-3 text-sm leading-6 text-muted">
        <p>{t('ssh.description')}</p>
        <p className="text-xs">{t('ssh.risk')}</p>
      </div>
      {notice ? (
        <p
          role="status"
          className="mb-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:border-emerald-500/25 dark:bg-emerald-500/10 dark:text-emerald-300"
        >
          {notice}
        </p>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
        >
          <span className="min-w-0 break-words">{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="ui-button ui-focus-ring min-h-11 shrink-0"
          >
            {t('common.retry')}
          </button>
        </div>
      ) : null}
      {!configured ? <p className="pb-3 text-sm text-muted">{t('ssh.unconfigured')}</p> : null}
      {loading && servers.length === 0 ? (
        <p role="status" className="border-t border-border py-3 text-sm text-muted">
          {t('common.loading')}
        </p>
      ) : servers.length === 0 && configured && !error ? (
        <p className="border-t border-border py-3 text-sm text-muted">{t('ssh.empty')}</p>
      ) : null}
      <ul>
        {servers.map((server) => (
          <li
            key={server.id}
            className="flex flex-col justify-center gap-3 border-t border-border py-3 lg:flex-row lg:items-center"
          >
            <div className="flex min-w-0 flex-1 items-start gap-3 sm:gap-4">
              <ServerIcon />
              <div className="min-w-0">
                <h3 className="flex flex-wrap items-center gap-2 text-sm font-semibold text-foreground">
                  {server.name}
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      server.status === 'ready'
                        ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                        : 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300'
                    }`}
                  >
                    {server.status === 'ready' ? t('ssh.statusReady') : t('ssh.statusPending')}
                  </span>
                </h3>
                <p className="mt-1 break-words font-mono text-xs text-muted">
                  {server.username}@{server.address}:{server.port}
                  {server.pinned_address && server.pinned_address !== server.address
                    ? ` (${server.pinned_address})`
                    : ''}
                </p>
                {server.description ? (
                  <p className="mt-1 break-words text-sm text-muted">{server.description}</p>
                ) : null}
                {server.last_check_message ? (
                  <p className="mt-1 break-words text-sm text-amber-700 dark:text-amber-400">
                    {server.last_check_message}
                  </p>
                ) : null}
              </div>
            </div>
            <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-2 lg:justify-end">
              {confirming === server.id ? (
                <div className="flex flex-col gap-2 lg:max-w-sm">
                  <p className="text-sm text-foreground">
                    {t('ssh.deleteConfirm', { name: server.name })}
                  </p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      disabled={busy === server.id}
                      onClick={() => void remove(server)}
                      className="ui-button ui-button-secondary min-h-11 text-status-error hover:bg-red-50 dark:hover:bg-red-500/10"
                    >
                      {t('ssh.delete')}
                    </button>
                    <button
                      type="button"
                      disabled={busy === server.id}
                      onClick={() => setConfirming(null)}
                      className={ACTION}
                    >
                      {t('ssh.cancel')}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <button
                    type="button"
                    disabled={busy === server.id}
                    onClick={() => setChecking(server)}
                    className={ACTION}
                  >
                    {server.pending_public_key ? t('ssh.testNewKey') : t('ssh.check')}
                  </button>
                  <details className="group relative">
                    <summary className="ui-button ui-button-secondary min-h-11 cursor-pointer list-none [&::-webkit-details-marker]:hidden">
                      {t('ssh.manage')}
                    </summary>
                    <div className="ui-card absolute right-0 z-10 mt-2 w-48 bg-surface p-1.5 shadow-lg">
                      <button
                        type="button"
                        className={MENU_ITEM}
                        onClick={() => setEditing(server)}
                      >
                        {t('ssh.edit')}
                      </button>
                      <button
                        type="button"
                        disabled={busy === server.id}
                        className={MENU_ITEM}
                        onClick={() => void rotate(server)}
                      >
                        {t('ssh.rotate')}
                      </button>
                      <button
                        type="button"
                        className={`${MENU_ITEM} text-status-error`}
                        onClick={() => setConfirming(server.id)}
                      >
                        {t('ssh.delete')}
                      </button>
                    </div>
                  </details>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      {editing ? (
        <SshServerDialog
          server={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(server, created) => {
            replace(server);
            setEditing(null);
            if (created || server.status !== 'ready') setChecking(server);
          }}
        />
      ) : null}
      {checking ? (
        <SshCheckDialog
          server={checking}
          onClose={() => setChecking(null)}
          onChanged={replace}
          refresh={async () => (await load())?.find((row) => row.id === checking.id)}
        />
      ) : null}
    </section>
  );
}
