'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { getAgentConfig } from '@/lib/api/agents';
import {
  copyEnvironment,
  deleteEnvironment,
  listEnvironments,
  setEnvironmentDefault,
} from '@/lib/api/environments';
import type { EnvironmentConfig } from '@/lib/api/environments';
import { EnvironmentEditor } from './EnvironmentEditor';

export function emptyEnvironment(repo: string | null): EnvironmentConfig {
  return {
    id: null,
    repo,
    name: repo ? '' : 'Global',
    revision: 0,
    variables: [],
    setup_script: '',
    start_script: '',
    verify_script: '',
    actions: [],
  };
}
export function EnvironmentManager({
  repo: onlyRepo,
  onChanged,
}: {
  repo?: string;
  onChanged?: () => void;
}) {
  const t = useT();
  const [items, setItems] = useState<EnvironmentConfig[]>([]);
  const [globals, setGlobals] = useState(emptyEnvironment(null));
  const [repos, setRepos] = useState<string[]>(onlyRepo ? [onlyRepo] : []);
  const [repo, setRepo] = useState(onlyRepo ?? '');
  const [editing, setEditing] = useState<EnvironmentConfig | null>(null);
  const [editorKey, setEditorKey] = useState(0);
  const edit = (value: EnvironmentConfig) => {
    setEditing(value);
    setEditorKey((key) => key + 1);
  };
  const [error, setError] = useState('');
  const [copying, setCopying] = useState<EnvironmentConfig | null>(null);
  const [copyName, setCopyName] = useState('');
  const [copyRepo, setCopyRepo] = useState('');
  const copyRequest = useRef<{ fingerprint: string; key: string } | null>(null);
  const load = useCallback(async () => {
    const result = await listEnvironments(onlyRepo);
    setItems(result.environments);
    setGlobals(result.globals);
  }, [onlyRepo]);
  useEffect(() => {
    let active = true;
    void load().catch((e) => {
      if (active) setError(e instanceof Error ? e.message : String(e));
    });
    if (!onlyRepo)
      void getAgentConfig()
        .then((config) => {
          if (active) setRepos(config.repos);
        })
        .catch(() => {});
    return () => {
      active = false;
    };
  }, [load, onlyRepo]);
  async function perform(action: () => Promise<unknown>) {
    setError('');
    try {
      await action();
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <section className="space-y-4" aria-label={t('environment.title')}>
      <div>
        <h2 className="text-lg font-semibold">{t('environment.title')}</h2>
        <p className="mt-1 text-sm text-muted">{t('environment.description')}</p>
      </div>
      {!onlyRepo && (
        <details className="rounded-xl border border-border p-3">
          <summary>{t('environment.globalVariables')}</summary>
          <div className="mt-3">
            <EnvironmentEditor
              key={globals.id ?? 'globals'}
              initial={globals}
              globals={globals}
              onSaved={(value) => {
                setGlobals(value);
                onChanged?.();
              }}
            />
          </div>
        </details>
      )}
      <div className="flex gap-2">
        <select
          aria-label={t('environment.repository')}
          className="ui-input min-w-0 flex-1"
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
          disabled={!!onlyRepo}
        >
          <option value="">{t('environment.chooseRepository')}</option>
          {repos.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="ui-button ui-button-secondary"
          disabled={!repo}
          onClick={() => edit(emptyEnvironment(repo))}
        >
          {t('environment.new')}
        </button>
      </div>
      {items.map((item) => (
        <div
          key={item.id}
          className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border p-3"
        >
          <div>
            <p className="text-sm font-medium">
              {item.name} · v{item.revision}
              {item.is_default ? ` · ${t('environment.default')}` : ''}
            </p>
            <p className="text-xs text-muted">
              {item.repo} · {item.variables.length} {t('environment.variables')}
              {item.latest_test
                ? ` · ${item.latest_test.state}${item.latest_test.revision !== item.revision || item.latest_test.global_revision !== globals.revision ? ` (${t('environment.olderTest')})` : ''}`
                : ''}
            </p>
          </div>
          <div className="flex flex-wrap gap-1">
            <button type="button" className="ui-button ui-button-ghost" onClick={() => edit(item)}>
              {t('environment.edit')}
            </button>
            <button
              type="button"
              className="ui-button ui-button-ghost"
              onClick={() => {
                copyRequest.current = null;
                setCopying(item);
                setCopyName(`${item.name} ${t('environment.copy')}`);
                setCopyRepo(item.repo ?? '');
              }}
            >
              {t('environment.copy')}
            </button>
            <button
              type="button"
              className="ui-button ui-button-ghost"
              disabled={item.is_default}
              onClick={() =>
                void perform(() =>
                  setEnvironmentDefault(item.repo!, item.id, item.default_revision ?? 0),
                )
              }
            >
              {t('environment.makeDefault')}
            </button>
            <button
              type="button"
              className="ui-button ui-button-ghost"
              onClick={() => void perform(() => deleteEnvironment(item.id!, item.revision))}
            >
              {t('environment.remove')}
            </button>
          </div>
        </div>
      ))}
      {copying && (
        <div className="space-y-2 rounded-lg border border-border p-3">
          <label className="block text-sm">
            {t('environment.name')}
            <input
              className="ui-input w-full"
              value={copyName}
              onChange={(e) => setCopyName(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            {t('environment.repository')}
            <select
              className="ui-input w-full"
              value={copyRepo}
              onChange={(e) => setCopyRepo(e.target.value)}
            >
              {repos.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <p className="text-xs text-muted">{t('environment.copySecrets')}</p>
          <button
            type="button"
            className="ui-button ui-button-secondary"
            onClick={() =>
              void perform(async () => {
                const fingerprint = JSON.stringify([
                  copying.id,
                  copying.revision,
                  copyRepo,
                  copyName.trim(),
                ]);
                if (copyRequest.current?.fingerprint !== fingerprint)
                  copyRequest.current = { fingerprint, key: crypto.randomUUID() };
                edit(
                  await copyEnvironment(
                    copying.id!,
                    copying.revision,
                    copyRepo,
                    copyName,
                    copyRequest.current.key,
                  ),
                );
                setCopying(null);
              })
            }
          >
            {t('environment.copy')}
          </button>
          <button
            type="button"
            className="ui-button ui-button-ghost"
            onClick={() => setCopying(null)}
          >
            {t('environment.close')}
          </button>
        </div>
      )}
      {editing && (
        <EnvironmentEditor
          key={editorKey}
          initial={editing}
          globals={globals}
          onSaved={(next) => {
            setEditing(next);
            void load();
            onChanged?.();
          }}
          onClose={() => setEditing(null)}
        />
      )}
      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
