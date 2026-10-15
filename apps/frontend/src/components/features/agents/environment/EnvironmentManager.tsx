'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { getAgentConfig } from '@/lib/api/agents';
import {
  copyEnvironment,
  deleteEnvironment,
  deleteEnvironmentSnapshot,
  listEnvironments,
  rebuildEnvironmentSnapshot,
  setEnvironmentDefault,
} from '@/lib/api/environments';
import type { EnvironmentConfig, EnvironmentTeam } from '@/lib/api/environments';
import { listTeams } from '@/lib/api/teams';
import { EnvironmentDialog, OwnerSelect } from './EnvironmentDialog';
import { Badge, SnapshotBadge, TeamBadge, TestBadge } from './EnvironmentStatus';

export function emptyEnvironment(repo: string | null): EnvironmentConfig {
  return {
    id: null,
    repo,
    name: repo === null ? 'Global' : '',
    revision: 0,
    variables: [],
    setup_script: '',
    start_script: '',
    verify_script: '',
    actions: [],
  };
}

type RowAction = 'copy' | 'default' | 'rebuild' | 'discard' | 'remove';

/** The less frequent things to do with an environment, behind one button per row. */
function RowMenu({
  name,
  actions,
  onAction,
}: {
  name: string;
  actions: { action: RowAction; label: string }[];
  onAction: (action: RowAction) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    const outside = (event: PointerEvent) => {
      if (
        !menu.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  function close() {
    setOpen(false);
    trigger.current?.focus();
  }
  const label = t('environment.manageItem', { name });
  return (
    <div className="relative">
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen(!open)}
        className="ui-icon-button ui-button-ghost"
      >
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="5" r="1.75" />
          <circle cx="12" cy="12" r="1.75" />
          <circle cx="12" cy="19" r="1.75" />
        </svg>
      </button>
      {open ? (
        <div
          id={id}
          ref={menu}
          role="menu"
          aria-label={label}
          className="ui-popover absolute right-0 top-full z-20 mt-1 w-44 rounded-xl border border-border bg-surface-raised p-1.5 shadow-card"
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
              close();
            } else if (event.key === 'Tab') {
              close();
            } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
              event.preventDefault();
              const items = [
                ...menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
              ];
              const current = items.indexOf(document.activeElement as HTMLButtonElement);
              const next =
                event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? items.length - 1
                    : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) %
                      items.length;
              items[next]?.focus();
            }
          }}
        >
          {actions.map(({ action, label: text }) => (
            <button
              key={action}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => {
                close();
                onAction(action);
              }}
              className={`block w-full rounded-md px-3 py-2 text-left text-sm hover:bg-surface-hover focus:bg-surface-hover focus:outline-none ${action === 'remove' ? 'text-status-error' : 'text-foreground'}`}
            >
              {text}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** The teams whose environments you may add and change: those you own or administer. */
export async function editableTeams(): Promise<EnvironmentTeam[]> {
  const teams = await listTeams();
  return teams
    .filter((team) => team.role === 'owner' || team.role === 'admin')
    .map(({ id, name }) => ({ id, name }));
}

/**
 * The Environments card in My Settings: personal global variables, then each
 * repository's environments, your own and your teams'. Editing happens in
 * `EnvironmentDialog`; copying and removing are confirmed in place under their
 * row. A team's environment is changed only by its owner and admins; other
 * members can view, test, copy and choose it.
 */
export function EnvironmentManager() {
  const t = useT();
  const [items, setItems] = useState<EnvironmentConfig[] | null>(null);
  const [globals, setGlobals] = useState(emptyEnvironment(null));
  const [repos, setRepos] = useState<string[]>([]);
  const [editing, setEditing] = useState<EnvironmentConfig | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [copying, setCopying] = useState<EnvironmentConfig | null>(null);
  const [copyName, setCopyName] = useState('');
  const [copyRepo, setCopyRepo] = useState('');
  const [copyTeam, setCopyTeam] = useState('');
  const [teams, setTeams] = useState<EnvironmentTeam[]>([]);
  const copyRequest = useRef<{ fingerprint: string; key: string } | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [snapshotsEnabled, setSnapshotsEnabled] = useState(false);
  const load = useCallback(async () => {
    const result = await listEnvironments();
    setItems(result.environments);
    setGlobals(result.globals);
    setSnapshotsEnabled(Boolean(result.snapshots_enabled));
  }, []);
  // A snapshot builds on some host for a few minutes; follow it until it settles.
  const building = (items ?? []).some(
    (item) => item.snapshot?.state === 'queued' || item.snapshot?.state === 'building',
  );
  useEffect(() => {
    if (!building) return;
    const timer = setInterval(() => void load().catch(() => {}), 10_000);
    return () => clearInterval(timer);
  }, [building, load]);
  useEffect(() => {
    let active = true;
    void load().catch((e) => {
      if (active) setError(e instanceof Error ? e.message : String(e));
    });
    void getAgentConfig()
      .then((config) => {
        if (active) setRepos(config.repos);
      })
      .catch(() => {});
    // Without teams, everything here is simply your own.
    void editableTeams()
      .then((result) => {
        if (active) setTeams(result);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [load]);
  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const groups = new Map<string, EnvironmentConfig[]>();
  for (const item of items ?? []) {
    const repo = item.repo ?? '';
    groups.set(repo, [...(groups.get(repo) ?? []), item]);
  }
  const sorted = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  return (
    <section aria-labelledby="settings-environments" className="ui-card mt-4 bg-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1 basis-56">
          <h2 id="settings-environments" className="text-base font-semibold text-foreground">
            {t('environment.sectionTitle')}
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted">
            {t('environment.description')}
          </p>
        </div>
        <button
          type="button"
          className="ui-button ui-button-primary min-h-9"
          disabled={items === null}
          onClick={() => setEditing(emptyEnvironment(''))}
        >
          <span aria-hidden="true">+</span>
          {t('environment.new')}
        </button>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-foreground">{t('environment.globalVariables')}</p>
          <p className="text-xs text-muted">
            {t('environment.variableCount', { count: globals.variables.length })} ·{' '}
            {t('environment.globalScope')}
          </p>
        </div>
        <button
          type="button"
          className="ui-button ui-button-secondary"
          disabled={items === null}
          onClick={() => setEditing(globals)}
        >
          {t('environment.edit')}
        </button>
      </div>

      {items === null && !error ? (
        <p role="status" className="mt-4 text-sm text-muted">
          {t('environment.loading')}
        </p>
      ) : null}
      {items?.length === 0 ? (
        <p className="mt-4 rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
          {t('environment.empty')}
        </p>
      ) : null}
      {sorted.map(([repo, environments]) => (
        <div key={repo} className="mt-5">
          <h3 className="mb-1.5 truncate text-xs font-medium text-muted" title={repo}>
            {repo}
          </h3>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {environments.map((item) => {
              const editable = item.can_edit !== false;
              const menu: { action: RowAction; label: string }[] = [
                { action: 'copy', label: t('environment.copy') },
              ];
              if (!item.is_default)
                menu.push({ action: 'default', label: t('environment.makeDefault') });
              // A team's snapshots and the environment itself are every
              // member's, so only its owner and admins change them.
              if (editable && snapshotsEnabled && item.setup_script)
                menu.push({ action: 'rebuild', label: t('environment.snapshot.rebuild') });
              if (editable && snapshotsEnabled && item.snapshot?.state === 'ready')
                menu.push({ action: 'discard', label: t('environment.snapshot.delete') });
              if (editable) menu.push({ action: 'remove', label: t('environment.remove') });
              return (
                <li key={item.id}>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
                    <button
                      type="button"
                      className="group min-w-0 flex-1 text-left"
                      aria-label={t(editable ? 'environment.editItem' : 'environment.viewItem', {
                        name: item.name,
                      })}
                      onClick={() => setEditing(item)}
                    >
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="truncate text-sm font-medium text-foreground group-hover:text-accent">
                          {item.name}
                        </span>
                        {item.team ? <TeamBadge name={item.team.name} /> : null}
                        {item.is_default ? (
                          <Badge tone="neutral" dot={false}>
                            {t('environment.default')}
                          </Badge>
                        ) : null}
                        <TestBadge environment={item} globalRevision={globals.revision} />
                        {snapshotsEnabled && item.setup_script ? (
                          <SnapshotBadge snapshot={item.snapshot} />
                        ) : null}
                      </span>
                      <span className="mt-0.5 block text-xs text-muted">
                        {t('environment.variableCount', { count: item.variables.length })} ·{' '}
                        {t('environment.versionLabel', { revision: item.revision })}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="ui-button ui-button-secondary"
                      onClick={() => setEditing(item)}
                    >
                      {t(editable ? 'environment.edit' : 'environment.view')}
                    </button>
                    <RowMenu
                      name={item.name}
                      actions={menu}
                      onAction={(action) => {
                        setRemoving(null);
                        setCopying(null);
                        if (action === 'copy') {
                          copyRequest.current = null;
                          setCopying(item);
                          setCopyName(t('environment.copyName', { name: item.name }));
                          setCopyRepo(item.repo ?? '');
                          setCopyTeam('');
                        } else if (action === 'default') {
                          void perform(() =>
                            setEnvironmentDefault(item.repo!, item.id, item.default_revision ?? 0),
                          );
                        } else if (action === 'rebuild') {
                          void perform(() => rebuildEnvironmentSnapshot(item.id!));
                        } else if (action === 'discard') {
                          void perform(() => deleteEnvironmentSnapshot(item.id!));
                        } else {
                          setRemoving(item.id);
                        }
                      }}
                    />
                  </div>
                  {removing === item.id ? (
                    <div className="flex flex-wrap items-center gap-2 border-t border-border bg-surface-muted/50 px-3 py-2.5">
                      <p className="mr-auto text-sm">
                        {t('environment.removeConfirm', { name: item.name })}
                      </p>
                      <button
                        type="button"
                        className="ui-button ui-button-ghost"
                        onClick={() => setRemoving(null)}
                      >
                        {t('environment.cancel')}
                      </button>
                      <button
                        type="button"
                        className="ui-button bg-status-error text-white hover:bg-status-error/90"
                        disabled={busy}
                        onClick={() =>
                          void perform(async () => {
                            await deleteEnvironment(item.id!, item.revision);
                            setRemoving(null);
                          })
                        }
                      >
                        {t('environment.remove')}
                      </button>
                    </div>
                  ) : null}
                  {copying?.id === item.id ? (
                    <div className="space-y-2 border-t border-border bg-surface-muted/50 px-3 py-3">
                      <div
                        className={`grid gap-2 ${teams.length ? 'sm:grid-cols-3' : 'sm:grid-cols-2'}`}
                      >
                        <label className="space-y-1 text-sm">
                          <span className="text-muted">{t('environment.name')}</span>
                          <input
                            className="ui-input w-full"
                            value={copyName}
                            onChange={(e) => setCopyName(e.target.value)}
                          />
                        </label>
                        <label className="space-y-1 text-sm">
                          <span className="text-muted">{t('environment.repository')}</span>
                          <select
                            className="ui-input w-full"
                            value={copyRepo}
                            onChange={(e) => setCopyRepo(e.target.value)}
                          >
                            {[...new Set([copyRepo, ...repos])].map((r) => (
                              <option key={r} value={r}>
                                {r}
                              </option>
                            ))}
                          </select>
                        </label>
                        {teams.length ? (
                          <OwnerSelect
                            label={t('environment.copyTo')}
                            teams={teams}
                            value={copyTeam}
                            onChange={setCopyTeam}
                          />
                        ) : null}
                      </div>
                      <p className="text-xs text-muted">{t('environment.copySecrets')}</p>
                      <div className="flex justify-end gap-2">
                        <button
                          type="button"
                          className="ui-button ui-button-ghost"
                          onClick={() => setCopying(null)}
                        >
                          {t('environment.cancel')}
                        </button>
                        <button
                          type="button"
                          className="ui-button ui-button-primary"
                          disabled={busy || !copyName.trim()}
                          onClick={() =>
                            void perform(async () => {
                              // One key per distinct request, so a retry after
                              // a lost response cannot make a second copy.
                              const fingerprint = JSON.stringify([
                                copying.id,
                                copying.revision,
                                copyRepo,
                                copyName.trim(),
                                copyTeam,
                              ]);
                              if (copyRequest.current?.fingerprint !== fingerprint)
                                copyRequest.current = { fingerprint, key: crypto.randomUUID() };
                              const copy = await copyEnvironment(
                                copying.id!,
                                copying.revision,
                                copyRepo,
                                copyName,
                                copyRequest.current.key,
                                copyTeam || null,
                              );
                              setCopying(null);
                              setEditing(copy);
                            })
                          }
                        >
                          {t('environment.copy')}
                        </button>
                      </div>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
      {error ? (
        <p role="alert" className="mt-3 text-sm text-status-error">
          {error}
        </p>
      ) : null}
      {editing ? (
        <EnvironmentDialog
          initial={editing}
          globals={globals}
          repos={repos}
          teams={teams}
          snapshotsEnabled={snapshotsEnabled}
          onSaved={(next) => {
            if (next.repo === null) setGlobals(next);
            void load().catch(() => {});
          }}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </section>
  );
}
