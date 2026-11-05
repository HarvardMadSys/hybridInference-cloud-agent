'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
import { getGlobalEnvironment, listEnvironments } from '@/lib/api/environments';
import type { EnvironmentConfig, EnvironmentVariable } from '@/lib/api/environments';
import { EnvironmentDialog } from './EnvironmentDialog';
import { emptyEnvironment } from './EnvironmentManager';
import { Badge, TeamBadge, TestBadge } from './EnvironmentStatus';

export interface EnvironmentSelection {
  environment_id: string | null;
  environment_revision: number;
  global_revision: number;
}

/**
 * What the task will run with, after an environment's variables replace global
 * ones. A team environment's installation is the team's alone, so your
 * install-only globals do not reach it.
 */
function EffectiveConfiguration({
  globals,
  environment,
}: {
  globals: EnvironmentConfig;
  environment?: EnvironmentConfig;
}) {
  const t = useT();
  const effective = new Map<string, EnvironmentVariable & { from: string }>(
    globals.variables
      .filter((v) => !environment?.team || v.phase !== 'setup')
      .map((v) => [v.name, { ...v, from: t('environment.sourceGlobal') }]),
  );
  environment?.variables.forEach((v) => effective.set(v.name, { ...v, from: environment.name }));
  const scripts = (
    [
      ['setup_script', 'environment.installScript'],
      ['start_script', 'environment.startScript'],
    ] as const
  ).filter(([key]) => environment?.[key]);
  return (
    <div className="space-y-3">
      {effective.size ? (
        <ul className="divide-y divide-border rounded-lg border border-border text-xs">
          {Array.from(effective.values()).map((v) => (
            <li key={v.name} className="flex items-center gap-3 px-3 py-1.5">
              <code className="min-w-0 flex-1 truncate text-foreground">{v.name}</code>
              <span className="text-muted">
                {v.secret ? t('environment.secret') : t('environment.ordinary')}
              </span>
              <span className="w-24 truncate text-right text-muted" title={v.from}>
                {v.from}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted">{t('environment.noVariables')}</p>
      )}
      {scripts.map(([key, label]) => (
        <div key={key}>
          <p className="mb-1 text-xs text-muted">{t(label)}</p>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-surface-muted p-2 font-mono text-xs">
            {environment![key]}
          </pre>
        </div>
      ))}
    </div>
  );
}

export function EnvironmentPicker({
  repo,
  temporary = false,
  open,
  onClose,
  onChange,
  onStatusChange,
  onSummaryChange,
}: {
  repo: string;
  /**
   * A temporary workspace: environments belong to one repository, so it runs
   * on the platform's base with the owner's global variables.
   */
  temporary?: boolean;
  open: boolean;
  onClose: () => void;
  onChange: (selection: EnvironmentSelection | null) => void;
  onStatusChange?: (status: 'loading' | 'ready' | 'error') => void;
  onSummaryChange?: (summary: string) => void;
}) {
  const t = useT();
  const titleId = useId();
  const [items, setItems] = useState<EnvironmentConfig[]>([]);
  const [globals, setGlobals] = useState<EnvironmentConfig | null>(null);
  const [selected, setSelected] = useState('');
  // The dialog keeps the global variables it opened with: a reload after its
  // save clears `globals` while the list is fetched again.
  const [editing, setEditing] = useState<{
    config: EnvironmentConfig;
    globals: EnvironmentConfig;
  } | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const initializedRepo = useRef<string | null>(null);
  // An environment created from here is the one the task should use.
  const created = useRef<string | null>(null);
  const scope = temporary ? ':temporary' : repo;
  const panel = useRef<HTMLDivElement>(null);
  useModalDialog(panel, {
    open: open && !!scope,
    onClose,
    initialFocus: temporary ? '[data-environment-done]' : 'input[type="radio"]:checked',
  });
  useEffect(() => {
    let active = true;
    onChange(null);
    onStatusChange?.(scope ? 'loading' : 'ready');
    setError('');
    setGlobals(null);
    if (!scope) return;
    if (temporary) {
      void getGlobalEnvironment()
        .then((result) => {
          if (!active) return;
          initializedRepo.current = scope;
          onStatusChange?.('ready');
          setItems([]);
          setSelected('');
          setGlobals(result);
        })
        .catch((e) => {
          if (active) {
            onStatusChange?.('error');
            setError(e instanceof Error ? e.message : String(e));
          }
        });
      return () => {
        active = false;
      };
    }
    void listEnvironments(repo)
      .then((result) => {
        if (!active) return;
        const initialLoad = initializedRepo.current !== repo;
        initializedRepo.current = repo;
        onStatusChange?.('ready');
        setItems(result.environments);
        setGlobals(result.globals);
        const exists = (id: string | null) => result.environments.some((e) => e.id === id);
        const pending = created.current;
        created.current = null;
        setSelected((previous) =>
          pending && exists(pending)
            ? pending
            : initialLoad
              ? (result.environments.find((e) => e.is_default)?.id ?? '')
              : exists(previous)
                ? previous
                : '',
        );
      })
      .catch((e) => {
        if (active) {
          onStatusChange?.('error');
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      active = false;
    };
  }, [repo, scope, temporary, reload, onChange, onStatusChange]);
  useEffect(() => {
    if (!globals) return;
    const env = items.find((e) => e.id === selected);
    onChange({
      environment_id: selected || null,
      environment_revision: env?.revision ?? 0,
      global_revision: globals.revision,
    });
  }, [globals, items, selected, onChange]);
  const env = items.find((e) => e.id === selected);
  useEffect(() => {
    onSummaryChange?.(globals ? (env?.name ?? t('environment.none')) : '');
  }, [globals, env, onSummaryChange, t]);
  const errorNotice = error ? (
    <p role="alert" className="text-sm text-status-error">
      {t('environment.loadFailed')} {error}
      <button
        type="button"
        className="ui-button ui-button-ghost"
        onClick={() => setReload((v) => v + 1)}
      >
        {t('environment.retry')}
      </button>
    </p>
  ) : null;
  if (!scope) return null;
  if (!open) return errorNotice ? <div className="px-3 py-2">{errorNotice}</div> : null;
  if (typeof document === 'undefined') return null;
  const option = (id: string, title: string, children: ReactNode) => (
    <label
      key={id || 'none'}
      className={`flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition-colors ${
        selected === id
          ? 'border-accent bg-accent-subtle/40'
          : 'border-border hover:bg-surface-hover'
      }`}
    >
      <input
        type="radio"
        name={`${titleId}-environment`}
        value={id}
        checked={selected === id}
        disabled={!globals}
        onChange={() => setSelected(id)}
        className="mt-1 accent-[rgb(var(--ui-accent))]"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium text-foreground">{title}</span>
        {children}
      </span>
    </label>
  );
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('environment.close')}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card"
      >
        <div className="shrink-0 px-6 pb-3 pt-5">
          <h2 id={titleId} className="text-lg font-semibold text-foreground">
            {t('environment.title')}
          </h2>
          <p
            className="mt-0.5 truncate text-sm text-muted"
            title={temporary ? t('temporaryWorkspace.name') : repo}
          >
            {temporary ? t('temporaryWorkspace.name') : repo}
          </p>
          <p className="mt-3 text-sm text-muted">
            {t(temporary ? 'environment.temporaryHint' : 'environment.automaticHint')}
          </p>
        </div>
        <div className="min-h-0 space-y-4 overflow-y-auto px-6 pb-5 text-sm">
          {errorNotice}
          {!globals && !error ? (
            <p role="status" className="text-muted">
              {t('environment.loading')}
            </p>
          ) : null}
          {temporary ? (
            globals ? (
              <EffectiveConfiguration globals={globals} />
            ) : null
          ) : (
            <fieldset className="space-y-2">
              <legend className="sr-only">{t('environment.choose')}</legend>
              {items.map((item) =>
                option(
                  item.id!,
                  item.name,
                  <span className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                    {item.team ? <TeamBadge name={item.team.name} /> : null}
                    {item.is_default ? (
                      <Badge tone="neutral" dot={false}>
                        {t('environment.default')}
                      </Badge>
                    ) : null}
                    {globals ? (
                      <TestBadge environment={item} globalRevision={globals.revision} />
                    ) : null}
                    <span>{t('environment.variableCount', { count: item.variables.length })}</span>
                    {item.setup_script ? <span>· {t('environment.installScript')}</span> : null}
                    {item.start_script ? <span>· {t('environment.startScript')}</span> : null}
                  </span>,
                ),
              )}
              {option(
                '',
                t('environment.none'),
                <span className="mt-0.5 block text-xs text-muted">
                  {t('environment.noneDescription')}
                </span>,
              )}
            </fieldset>
          )}
          {!temporary && globals ? (
            <details className="rounded-lg border border-border">
              <summary className="cursor-pointer px-3 py-2 font-medium hover:bg-surface-hover">
                {t('environment.details')}
              </summary>
              <div className="border-t border-border p-3">
                <EffectiveConfiguration globals={globals} environment={env} />
              </div>
            </details>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-6 py-3">
          {temporary ? (
            <button
              type="button"
              className="ui-button ui-button-ghost"
              disabled={!globals}
              onClick={() => globals && setEditing({ config: globals, globals })}
            >
              {t('environment.editGlobals')}
            </button>
          ) : (
            <>
              <button
                type="button"
                className="ui-button ui-button-ghost"
                disabled={!globals}
                onClick={() => globals && setEditing({ config: emptyEnvironment(repo), globals })}
              >
                <span aria-hidden="true">+</span>
                {t('environment.new')}
              </button>
              {env ? (
                <button
                  type="button"
                  className="ui-button ui-button-ghost"
                  onClick={() => globals && setEditing({ config: env, globals })}
                >
                  {t(env.can_edit === false ? 'environment.viewItem' : 'environment.editItem', {
                    name: env.name,
                  })}
                </button>
              ) : null}
            </>
          )}
          <button
            type="button"
            data-environment-done
            onClick={onClose}
            className="ui-button ui-button-primary ml-auto"
          >
            {t('environment.done')}
          </button>
        </div>
      </div>
      {editing ? (
        <EnvironmentDialog
          initial={editing.config}
          globals={editing.globals}
          onSaved={(next) => {
            if (next.repo !== null && !editing.config.id) created.current = next.id;
            setReload((v) => v + 1);
          }}
          onClose={() => {
            setEditing(null);
            // A test run in the editor saves nothing, but changes its result here.
            setReload((v) => v + 1);
          }}
        />
      ) : null}
    </div>,
    document.body,
  );
}
