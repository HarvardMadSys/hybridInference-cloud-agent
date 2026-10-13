'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers';
import {
  cancelEnvironmentRun,
  getEnvironmentHistory,
  getEnvironmentRun,
  importEnvironmentVariables,
  previewEnvironmentImport,
  restoreEnvironment,
  saveEnvironment,
  saveGlobalVariables,
  suggestEnvironmentScript,
  testEnvironment,
} from '@/lib/api/environments';
import type { EnvironmentConfig, EnvironmentRun, VariableEdit } from '@/lib/api/environments';
import { VariableEditor, variableEdits } from './VariableEditor';

const message = (cause: unknown) => (cause instanceof Error ? cause.message : String(cause));
export function EnvironmentSteps({
  run,
}: {
  run: { state?: string; steps: EnvironmentRun['steps']; detail?: string };
}) {
  const t = useT();
  return (
    <div className="space-y-2 text-sm" aria-live="polite">
      {run.state && (
        <p>
          {t('environment.status')}: {run.state}
        </p>
      )}
      {run.detail && <p className="text-red-600 dark:text-red-400">{run.detail}</p>}
      {run.steps.map((step, index) => (
        <details key={`${step.phase}-${index}`} className="rounded-lg border border-border p-2">
          <summary>
            {step.phase} · {step.state}
            {step.exit_code !== undefined ? ` · ${step.exit_code}` : ''}
            {step.duration_seconds !== undefined ? ` · ${step.duration_seconds.toFixed(1)}s` : ''}
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">
            {step.output}
          </pre>
        </details>
      ))}
    </div>
  );
}
export function EnvironmentEditor({
  initial,
  globals,
  onSaved,
  onClose,
}: {
  initial: EnvironmentConfig;
  globals: EnvironmentConfig;
  onSaved: (value: EnvironmentConfig) => void;
  onClose?: () => void;
}) {
  const t = useT();
  const [value, setValue] = useState(initial);
  const [variables, setVariables] = useState<VariableEdit[]>(() =>
    variableEdits(initial.variables),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [runVersion, setRunVersion] = useState<{ revision: number; globalRevision: number } | null>(
    null,
  );
  const [dotenv, setDotenv] = useState('');
  const [preview, setPreview] = useState<
    { name: string; operation: string; secret: boolean; secret_allowed: boolean }[] | null
  >(null);
  const [history, setHistory] = useState<EnvironmentConfig[] | null>(null);
  const [run, setRun] = useState<EnvironmentRun | null>(null);
  const global = value.repo === null;
  useEffect(() => {
    if (
      !run ||
      (!['queued', 'running'].includes(run.state) && (run.cleanup_complete !== false || !run.host))
    )
      return;
    let active = true;
    const timer = setInterval(() => {
      void getEnvironmentRun(run.id)
        .then((v) => {
          if (active) setRun(v);
        })
        .catch((e) => {
          if (active) setError(message(e));
        });
    }, 1000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [run]);
  function adopt(next: EnvironmentConfig) {
    changeValue(next);
    setVariables(variableEdits(next.variables));
    setSaved(true);
    setDirty(false);
    onSaved(next);
  }
  function changeValue(next: EnvironmentConfig) {
    setValue(next);
    setDirty(true);
    setSaved(false);
  }
  async function save() {
    const result = global
      ? await saveGlobalVariables(value.revision, variables)
      : await saveEnvironment({ ...value, variables });
    adopt(result);
    return result;
  }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      await action();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  const label = (key: 'setup_script' | 'start_script' | 'verify_script') =>
    t(
      key === 'setup_script'
        ? 'environment.installScript'
        : key === 'start_script'
          ? 'environment.startScript'
          : 'environment.verifyScript',
    );
  return (
    <section
      className="space-y-5 rounded-xl border border-border bg-surface p-4"
      aria-label={t('environment.editor')}
    >
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">
          {global ? t('environment.globalVariables') : t('environment.editor')} · v{value.revision}
        </h3>
        {onClose && (
          <button type="button" className="ui-button ui-button-ghost" onClick={onClose}>
            {t('environment.close')}
          </button>
        )}
      </div>
      {!global && (
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="space-y-1 text-sm">
            {t('environment.name')}
            <input
              className="ui-input w-full"
              value={value.name}
              onChange={(e) => {
                changeValue({ ...value, name: e.target.value });
                setSaved(false);
                setDirty(true);
              }}
            />
          </label>
          <div className="pt-6 text-sm text-muted">{value.repo}</div>
        </div>
      )}
      <VariableEditor
        values={variables}
        onChange={(next) => {
          setVariables(next);
          setSaved(false);
          setDirty(true);
        }}
        inherited={global ? [] : globals.variables}
      />
      <details>
        <summary className="text-sm font-medium">{t('environment.importEnv')}</summary>
        <textarea
          aria-label={t('environment.dotenv')}
          className="ui-input mt-2 min-h-28 w-full font-mono text-sm"
          value={dotenv}
          onChange={(e) => {
            setDotenv(e.target.value);
            setPreview(null);
          }}
          autoComplete="off"
        />
        <button
          type="button"
          disabled={busy || !dotenv || !value.id}
          className="ui-button ui-button-secondary mt-2"
          onClick={() =>
            void perform(async () => {
              setPreview(
                (await previewEnvironmentImport(global ? null : value.id, value.revision, dotenv))
                  .variables,
              );
            })
          }
        >
          {t('environment.previewImport')}
        </button>
        {!value.id && <p className="text-xs text-muted">{t('environment.saveBeforeImport')}</p>}
        {preview && (
          <div className="mt-2 space-y-2">
            <ul>
              {preview.map((v) => (
                <li key={v.name}>
                  <code>{v.name}</code> · {v.operation}
                  <select
                    aria-label={`${t('environment.type')} ${v.name}`}
                    value={v.secret ? 'secret' : 'ordinary'}
                    onChange={(event) =>
                      setPreview(
                        preview.map((item) =>
                          item.name === v.name
                            ? { ...item, secret: event.target.value === 'secret' }
                            : item,
                        ),
                      )
                    }
                  >
                    <option value="secret">{t('environment.secret')}</option>
                    <option value="ordinary">{t('environment.ordinary')}</option>
                  </select>
                  {v.secret && !v.secret_allowed && (
                    <span role="alert">{t('environment.shortSecret')}</span>
                  )}
                </li>
              ))}
            </ul>
            <button
              type="button"
              disabled={busy || preview.some((item) => item.secret && !item.secret_allowed)}
              className="ui-button ui-button-primary"
              onClick={() =>
                void perform(async () => {
                  const current = await save();
                  adopt(
                    await importEnvironmentVariables(
                      global ? null : current.id,
                      current.revision,
                      global ? current.revision : globals.revision,
                      dotenv,
                      preview.filter((item) => !item.secret).map((item) => item.name),
                    ),
                  );
                  setDotenv('');
                  setPreview(null);
                })
              }
            >
              {t('environment.confirmImport')}
            </button>
          </div>
        )}
      </details>
      {!global && (
        <>
          {(['setup_script', 'start_script', 'verify_script'] as const).map((key) => (
            <label className="block space-y-2 text-sm" key={key}>
              {label(key)}
              <textarea
                className="ui-input min-h-28 w-full font-mono text-xs"
                value={value[key]}
                maxLength={8000}
                onChange={(e) => {
                  changeValue({ ...value, [key]: e.target.value });
                  setSaved(false);
                  setDirty(true);
                }}
              />
            </label>
          ))}
          <p className="text-xs text-muted">{t('environment.startHelp')}</p>
          <button
            type="button"
            className="ui-button ui-button-secondary"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                const result = await suggestEnvironmentScript(value.repo!);
                if (result.suggestions.length === 1)
                  changeValue({ ...value, setup_script: result.suggestions[0].script });
                else
                  setError(
                    result.suggestions.map((s) => `${s.file}: ${s.script}`).join('\n') ||
                      t('environment.noSuggestion'),
                  );
              })
            }
          >
            {t('environment.suggestScript')}
          </button>
          <div className="space-y-2">
            <h4 className="text-sm font-medium">{t('environment.actions')}</h4>
            {value.actions.map((action, index) => (
              <div className="space-y-2 rounded-lg border border-border p-3" key={action.id}>
                <input
                  aria-label={t('environment.actionName')}
                  className="ui-input w-full"
                  placeholder={t('environment.actionName')}
                  value={action.name}
                  onChange={(e) =>
                    changeValue({
                      ...value,
                      actions: value.actions.map((a, i) =>
                        i === index ? { ...a, name: e.target.value } : a,
                      ),
                    })
                  }
                />
                <textarea
                  aria-label={t('environment.command')}
                  className="ui-input w-full font-mono text-xs"
                  value={action.command}
                  onChange={(e) =>
                    changeValue({
                      ...value,
                      actions: value.actions.map((a, i) =>
                        i === index ? { ...a, command: e.target.value } : a,
                      ),
                    })
                  }
                />
                <input
                  aria-label={t('environment.directory')}
                  className="ui-input w-full font-mono text-xs"
                  value={action.cwd}
                  onChange={(e) =>
                    changeValue({
                      ...value,
                      actions: value.actions.map((a, i) =>
                        i === index ? { ...a, cwd: e.target.value } : a,
                      ),
                    })
                  }
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="ui-button ui-button-ghost"
                    disabled={index === 0}
                    onClick={() => {
                      const actions = [...value.actions];
                      [actions[index - 1], actions[index]] = [actions[index], actions[index - 1]];
                      changeValue({ ...value, actions });
                    }}
                  >
                    {t('environment.moveUp')}
                  </button>
                  <button
                    type="button"
                    className="ui-button ui-button-ghost"
                    onClick={() =>
                      changeValue({
                        ...value,
                        actions: value.actions.filter((a) => a.id !== action.id),
                      })
                    }
                  >
                    {t('environment.remove')}
                  </button>
                </div>
              </div>
            ))}
            <button
              type="button"
              className="ui-button ui-button-secondary"
              onClick={() =>
                changeValue({
                  ...value,
                  actions: [
                    ...value.actions,
                    { id: crypto.randomUUID(), name: '', command: '', cwd: '.' },
                  ],
                })
              }
            >
              {t('environment.addAction')}
            </button>
          </div>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          className="ui-button ui-button-primary"
          onClick={() => void perform(save)}
        >
          {t('environment.save')}
        </button>
        {!global && (
          <button
            type="button"
            disabled={busy || (!!run && ['queued', 'running'].includes(run.state))}
            className="ui-button ui-button-secondary"
            onClick={() =>
              void perform(async () => {
                const current = await save();
                setRun(await testEnvironment(current.id!, current.revision, globals.revision));
                setRunVersion({ revision: current.revision, globalRevision: globals.revision });
              })
            }
          >
            {t('environment.saveAndTest')}
          </button>
        )}
        {value.id && (
          <button
            type="button"
            className="ui-button ui-button-secondary"
            disabled={busy}
            onClick={() =>
              void perform(async () =>
                setHistory((await getEnvironmentHistory(value.id!, global)).versions),
              )
            }
          >
            {t('environment.history')}
          </button>
        )}
        {saved && (
          <span className="self-center text-xs text-muted">{t('environment.savedForNew')}</span>
        )}
      </div>
      {error && (
        <p role="alert" className="whitespace-pre-wrap text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
      {history && (
        <div className="space-y-2">
          {history.map((version) => (
            <details className="rounded-lg border border-border p-2" key={version.revision}>
              <summary>
                v{version.revision} · {version.variables.length} {t('environment.variables')}
              </summary>
              <pre className="overflow-auto whitespace-pre-wrap text-xs">
                {version.setup_script}
                {'\n'}
                {version.start_script}
              </pre>
              <ul>
                {version.variables.map((v) => (
                  <li key={v.name}>
                    <code>{v.name}</code> · {v.secret ? t('environment.secretSet') : v.value} ·{' '}
                    {v.phase}
                  </li>
                ))}
              </ul>
              <button
                type="button"
                className="ui-button ui-button-secondary mt-2"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    adopt(
                      await restoreEnvironment(value.id!, value.revision, version.revision, global),
                    );
                    setHistory(null);
                  })
                }
              >
                {t('environment.restore')}
              </button>
            </details>
          ))}
        </div>
      )}
      {run && (
        <div className="space-y-2">
          {(dirty ||
            (runVersion &&
              (runVersion.revision !== value.revision ||
                runVersion.globalRevision !== globals.revision))) && (
            <p className="text-xs text-muted">{t('environment.olderTest')}</p>
          )}
          <EnvironmentSteps run={run} />
          <p className="break-all text-xs text-muted">
            {run.base_sha} · {run.image_digest}
          </p>
          {['queued', 'running'].includes(run.state) && (
            <button
              type="button"
              className="ui-button ui-button-secondary"
              onClick={() => void perform(async () => setRun(await cancelEnvironmentRun(run.id)))}
            >
              {t('environment.cancelTest')}
            </button>
          )}
        </div>
      )}
      <details className="text-xs text-muted">
        <summary>{t('environment.platformInfo')}</summary>
        <p className="mt-2">{t('environment.platformDescription')}</p>
      </details>
    </section>
  );
}
