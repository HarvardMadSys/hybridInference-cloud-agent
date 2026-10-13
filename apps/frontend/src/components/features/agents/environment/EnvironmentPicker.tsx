'use client';

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { listEnvironments } from '@/lib/api/environments';
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
  onChange,
  onStatusChange,
}: {
  repo: string;
  onChange: (selection: EnvironmentSelection | null) => void;
  onStatusChange?: (status: 'loading' | 'ready' | 'error') => void;
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
  useEffect(() => {
    let active = true;
    onChange(null);
    onStatusChange?.(repo ? 'loading' : 'ready');
    setError('');
    setGlobals(null);
    const changedRepo = previousRepo.current !== repo;
    previousRepo.current = repo;
    if (changedRepo) setOverrides([]);
    if (!repo) return;
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
  }, [repo, reload, onChange, onStatusChange]);
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
  return repo ? (
    <div className="space-y-2 border-t border-border px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex min-w-0 flex-1 items-center gap-2">
          {t('environment.title')}
          <select
            aria-label={t('environment.choose')}
            className="ui-input min-w-0 flex-1"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="">{t('environment.platformBase')}</option>
            {items.map((item) => (
              <option key={item.id} value={item.id!}>
                {item.name} · v{item.revision}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="ui-button ui-button-ghost"
          onClick={() => setManaging(!managing)}
        >
          {t('environment.manage')}
        </button>
      </div>
      <details>
        <summary>
          {t('environment.details')} · {effective.size} {t('environment.variables')}
        </summary>
        <ul className="mt-2 text-xs text-muted">
          {Array.from(effective.values()).map((v) => (
            <li key={v.name}>
              <code>{v.name}</code> ·{' '}
              {v.secret ? t('environment.secret') : t('environment.ordinary')} · {v.phase} ·{' '}
              {env?.variables.some((e) => e.name === v.name)
                ? env.name
                : t('environment.globalVariables')}
            </li>
          ))}
        </ul>
        {env && (
          <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap text-xs">
            {env.setup_script}\n{env.start_script}
          </pre>
        )}
      </details>
      <details>
        <summary>{t('environment.overrides')}</summary>
        <div className="mt-2">
          <VariableEditor values={overrides} onChange={setOverrides} />
        </div>
      </details>
      {managing && <EnvironmentManager repo={repo} onChanged={() => setReload((v) => v + 1)} />}
      {error && (
        <p role="alert" className="text-red-600 dark:text-red-400">
          {t('environment.loadFailed')} {error}
          <button
            type="button"
            className="ui-button ui-button-ghost"
            onClick={() => setReload((v) => v + 1)}
          >
            {t('environment.retry')}
          </button>
        </p>
      )}
    </div>
  ) : null;
}
