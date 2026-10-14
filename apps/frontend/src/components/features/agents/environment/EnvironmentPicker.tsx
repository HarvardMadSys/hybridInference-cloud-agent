'use client';

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
import { getGlobalEnvironment, listEnvironments } from '@/lib/api/environments';
import type { EnvironmentConfig, EnvironmentVariable, VariableEdit } from '@/lib/api/environments';
import { EnvironmentManager } from './EnvironmentManager';
import { VariableEditor } from './VariableEditor';

export interface EnvironmentSelection {
  environment_id: string | null;
  environment_revision: number;
  global_revision: number;
  environment_overrides: EnvironmentVariable[];
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
   * on the platform's base with the owner's global variables and this task's
   * own overrides.
   */
  temporary?: boolean;
  open: boolean;
  onClose: () => void;
  onChange: (selection: EnvironmentSelection | null) => void;
  onStatusChange?: (status: 'loading' | 'ready' | 'error') => void;
  onSummaryChange?: (summary: string) => void;
}) {
  const t = useT();
  const [items, setItems] = useState<EnvironmentConfig[]>([]);
  const [globals, setGlobals] = useState<EnvironmentConfig | null>(null);
  const [selected, setSelected] = useState('');
  const [overrides, setOverrides] = useState<VariableEdit[]>([]);
  const [managing, setManaging] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const previousRepo = useRef<string | null>(null);
  const initializedRepo = useRef<string | null>(null);
  // One scope key, so switching between a repository and a temporary
  // workspace clears overrides typed for the other.
  const scope = temporary ? ':temporary' : repo;
  const panel = useRef<HTMLDivElement>(null);
  useModalDialog(panel, {
    open: open && !!scope,
    onClose,
    initialFocus: temporary ? '[data-environment-done]' : 'select',
  });
  useEffect(() => {
    let active = true;
    onChange(null);
    onStatusChange?.(scope ? 'loading' : 'ready');
    setError('');
    setGlobals(null);
    const changedRepo = previousRepo.current !== scope;
    previousRepo.current = scope;
    if (changedRepo) setOverrides([]);
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
        setSelected((previous) =>
          initialLoad
            ? (result.environments.find((e) => e.is_default)?.id ?? '')
            : result.environments.some((e) => e.id === previous)
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
      environment_overrides: overrides
        .filter((v) => v.operation !== 'remove' && v.name.trim())
        .map((v) => ({ name: v.name, value: v.value ?? '', phase: v.phase, secret: v.secret })),
    });
  }, [globals, items, selected, overrides, onChange]);
  const env = items.find((e) => e.id === selected);
  useEffect(() => {
    if (!globals) {
      onSummaryChange?.('');
      return;
    }
    const name = env?.name ?? t('environment.platformBase');
    const count = overrides.filter((v) => v.operation !== 'remove' && v.name.trim()).length;
    onSummaryChange?.(count ? t('environment.selectionWithOverrides', { name, count }) : name);
  }, [globals, env, overrides, onSummaryChange, t]);
  const effective = new Map(
    (globals?.variables ?? []).map((v) => [v.name, { ...v, source: 'global' }]),
  );
  env?.variables.forEach((v) => effective.set(v.name, { ...v, source: 'environment' }));
  overrides
    .filter((v) => v.operation !== 'remove' && v.name)
    .forEach((v) =>
      effective.set(v.name, {
        name: v.name,
        value: v.value ?? null,
        secret: v.secret,
        phase: v.phase,
        source: 'override',
      }),
    );
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
        aria-label={t('environment.title')}
        className="relative flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card"
      >
        <div className="shrink-0 px-6 pb-4 pt-5">
          <h2 className="text-lg font-semibold text-foreground">{t('environment.title')}</h2>
          <p
            className="mt-1 truncate text-sm text-muted"
            title={temporary ? t('temporaryWorkspace.name') : repo}
          >
            {temporary ? t('temporaryWorkspace.name') : repo}
          </p>
          {temporary ? null : (
            <p className="mt-3 text-sm text-muted">{t('environment.automaticHint')}</p>
          )}
        </div>
        <div className="min-h-0 space-y-4 overflow-y-auto px-6 pb-5 text-sm">
          {errorNotice}
          {temporary ? (
            <p className="text-muted">{t('environment.platformBase')}</p>
          ) : (
            <div className="flex flex-wrap items-end gap-2">
              <label className="min-w-0 flex-1 space-y-2">
                <span className="block font-medium">{t('environment.choose')}</span>
                <select
                  className="ui-input w-full"
                  value={selected}
                  disabled={!globals}
                  onChange={(e) => setSelected(e.target.value)}
                >
                  <option value="">{t('environment.platformBase')}</option>
                  {items.map((item) => (
                    <option key={item.id} value={item.id!}>
                      {item.name} · v{item.revision}
                      {item.is_default ? ` · ${t('environment.default')}` : ''}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="ui-button ui-button-secondary"
                aria-expanded={managing}
                onClick={() => setManaging(!managing)}
              >
                {t('environment.manage')}
              </button>
            </div>
          )}
          {!globals && !error ? (
            <p role="status" className="text-muted">
              {t('environment.loading')}
            </p>
          ) : null}
          <details className="rounded-xl border border-border">
            <summary className="cursor-pointer px-4 py-3 font-medium">
              {t('environment.details')}{' '}
              <span className="ml-1 text-xs font-normal text-muted">
                {effective.size} {t('environment.variables')}
              </span>
            </summary>
            <div className="space-y-3 border-t border-border px-4 py-3">
              {effective.size ? (
                <ul className="space-y-2 text-xs text-muted">
                  {Array.from(effective.values()).map((v) => (
                    <li key={v.name} className="break-words">
                      <code className="text-foreground">{v.name}</code> ·{' '}
                      {v.secret ? t('environment.secret') : t('environment.ordinary')} · {v.phase} ·{' '}
                      {v.source === 'override'
                        ? t('environment.overrides')
                        : v.source === 'environment'
                          ? env?.name
                          : t('environment.globalVariables')}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-muted">{t('environment.noVariables')}</p>
              )}
              {env?.setup_script ? (
                <div>
                  <p className="mb-1 text-xs text-muted">{t('environment.installScript')}</p>
                  <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-muted p-3 text-xs">
                    {env.setup_script}
                  </pre>
                </div>
              ) : null}
              {env?.start_script ? (
                <div>
                  <p className="mb-1 text-xs text-muted">{t('environment.startScript')}</p>
                  <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-muted p-3 text-xs">
                    {env.start_script}
                  </pre>
                </div>
              ) : null}
            </div>
          </details>
          <details className="rounded-xl border border-border">
            <summary className="cursor-pointer px-4 py-3 font-medium">
              {t('environment.overrides')}
            </summary>
            <div className="border-t border-border px-4 py-3">
              <VariableEditor values={overrides} onChange={setOverrides} />
            </div>
          </details>
          {managing && !temporary && (
            <div className="border-t border-border pt-4">
              <EnvironmentManager repo={repo} onChanged={() => setReload((v) => v + 1)} />
            </div>
          )}
        </div>
        <div className="flex shrink-0 justify-end border-t border-border px-6 py-4">
          <button
            type="button"
            data-environment-done
            onClick={onClose}
            className="ui-button ui-button-primary"
          >
            {t('environment.done')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
