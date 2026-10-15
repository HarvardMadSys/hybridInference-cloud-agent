'use client';

import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
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
import type { TranslationKey } from '@/lib/i18n/translate';
import { ACTIVE_STATES, EnvironmentSteps, StateBadge } from './EnvironmentStatus';
import { RemoveIcon, VariableEditor, variableEdits } from './VariableEditor';

// The one place an environment is edited. Settings, the new-task picker and a
// conversation's Environment panel all open this dialog rather than each
// embedding their own editor, so the same environment looks and saves the
// same way wherever it is changed.

type Tab = 'variables' | 'scripts' | 'actions' | 'test' | 'history';
const TAB_LABELS: Record<Tab, TranslationKey> = {
  variables: 'environment.tab.variables',
  scripts: 'environment.tab.scripts',
  actions: 'environment.tab.actions',
  test: 'environment.tab.test',
  history: 'environment.tab.history',
};
const SCRIPTS = [
  ['setup_script', 'environment.installScript', 'environment.installHelp'],
  ['update_script', 'environment.updateScript', 'environment.updateHelp'],
  ['start_script', 'environment.startScript', 'environment.startHelp'],
  ['verify_script', 'environment.verifyScript', 'environment.verifyHelp'],
] as const;

const message = (cause: unknown) => (cause instanceof Error ? cause.message : String(cause));

function ArrowIcon({ up }: { up: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d={up ? 'm6 12 4-4 4 4' : 'm6 8 4 4 4-4'}
      />
    </svg>
  );
}

/**
 * Edit one environment, or the owner's global variables when `initial.repo`
 * is null. A new environment whose repository is still to be chosen has an
 * empty `repo` and is given `repos` to choose from.
 */
export function EnvironmentDialog({
  initial,
  globals,
  repos = [],
  snapshotsEnabled = false,
  onSaved,
  onClose,
}: {
  initial: EnvironmentConfig;
  globals: EnvironmentConfig;
  repos?: string[];
  /** Whether a passing test also saves the installation for new tasks. */
  snapshotsEnabled?: boolean;
  onSaved: (value: EnvironmentConfig) => void;
  onClose: () => void;
}) {
  const t = useT();
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const global = initial.repo === null;
  const [value, setValue] = useState(initial);
  const [variables, setVariables] = useState<VariableEdit[]>(() =>
    variableEdits(initial.variables),
  );
  const [tab, setTab] = useState<Tab>('variables');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const [importing, setImporting] = useState(false);
  const [dotenv, setDotenv] = useState('');
  const [preview, setPreview] = useState<
    { name: string; operation: string; secret: boolean; secret_allowed: boolean }[] | null
  >(null);
  const [suggestions, setSuggestions] = useState<{ file: string; script: string }[] | null>(null);
  const [history, setHistory] = useState<EnvironmentConfig[] | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);
  const [run, setRun] = useState<EnvironmentRun | null>(null);
  const [runVersion, setRunVersion] = useState<{ revision: number; global: number } | null>(
    initial.latest_test
      ? { revision: initial.latest_test.revision, global: initial.latest_test.global_revision }
      : null,
  );
  const isNew = !value.id;
  const tabs: Tab[] = global
    ? ['variables', ...(value.id ? (['history'] as const) : [])]
    : ['variables', 'scripts', 'actions', 'test', ...(value.id ? (['history'] as const) : [])];
  const tabButtons = useRef<Partial<Record<Tab, HTMLButtonElement | null>>>({});

  function requestClose() {
    if (busy) return;
    if (dirty) setConfirmingClose(true);
    else onClose();
  }
  useModalDialog(panel, {
    busy,
    onClose: requestClose,
    initialFocus:
      isNew && !global ? '[data-environment-name]' : '[role="tab"][aria-selected="true"]',
  });

  // The last test of the saved version, shown when the Test tab first opens.
  useEffect(() => {
    if (tab !== 'test' || run || !initial.latest_test) return;
    let active = true;
    void getEnvironmentRun(initial.latest_test.id)
      .then((result) => {
        if (active) setRun(result);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [tab, run, initial.latest_test]);
  useEffect(() => {
    if (tab !== 'history' || history || !value.id) return;
    let active = true;
    void getEnvironmentHistory(value.id, global)
      .then((result) => {
        if (active) setHistory(result.versions);
      })
      .catch((cause) => {
        if (active) setError(message(cause));
      });
    return () => {
      active = false;
    };
  }, [tab, history, value.id, global]);
  useEffect(() => {
    if (
      !run ||
      (!ACTIVE_STATES.includes(run.state) && (run.cleanup_complete !== false || !run.host))
    )
      return;
    let active = true;
    const timer = setInterval(() => {
      void getEnvironmentRun(run.id)
        .then((next) => {
          if (active) setRun(next);
        })
        .catch((cause) => {
          if (active) setError(message(cause));
        });
    }, 1000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [run]);

  function change(next: EnvironmentConfig) {
    setValue(next);
    setDirty(true);
    setSaved(false);
  }
  function adopt(next: EnvironmentConfig) {
    setValue(next);
    setVariables(variableEdits(next.variables));
    setDirty(false);
    setSaved(true);
    setHistory(null);
    onSaved(next);
  }
  async function save() {
    if (!global) {
      if (!value.name.trim()) throw new Error(t('environment.nameRequired'));
      if (!value.repo) throw new Error(t('environment.repositoryRequired'));
    }
    const result = global
      ? await saveGlobalVariables(value.revision, variables)
      : await saveEnvironment({ ...value, variables });
    adopt(result);
    return result;
  }
  // Saves only what has not been saved: an unchanged environment is already
  // the version a test or an import should use.
  const ensureSaved = async () => (dirty || !value.id ? save() : value);
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
  const saveAndTest = () =>
    perform(async () => {
      const current = await ensureSaved();
      setTab('test');
      setRun(await testEnvironment(current.id!, current.revision, globals.revision));
      setRunVersion({ revision: current.revision, global: globals.revision });
    });
  const testing = !!run && ACTIVE_STATES.includes(run.state);
  const testIsOlder =
    !!run &&
    (dirty ||
      (runVersion !== null &&
        (runVersion.revision !== value.revision || runVersion.global !== globals.revision)));

  function moveAction(index: number, by: number) {
    const actions = [...value.actions];
    [actions[index], actions[index + by]] = [actions[index + by], actions[index]];
    change({ ...value, actions });
  }

  const tabPanels: Record<Tab, () => ReactNode> = {
    variables: () => (
      <div className="space-y-4">
        <VariableEditor
          values={variables}
          onChange={(next) => {
            setVariables(next);
            setDirty(true);
            setSaved(false);
          }}
          inherited={global ? [] : globals.variables}
        />
        <div className="rounded-lg border border-border">
          <button
            type="button"
            aria-expanded={importing}
            onClick={() => setImporting(!importing)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium hover:bg-surface-hover"
          >
            {t('environment.importEnv')}
            <ArrowIcon up={importing} />
          </button>
          {importing ? (
            <div className="space-y-3 border-t border-border p-3">
              <p className="text-xs text-muted">{t('environment.importHint')}</p>
              <textarea
                aria-label={t('environment.dotenv')}
                className="ui-input min-h-28 w-full font-mono text-xs"
                placeholder={'API_URL=https://example.com\nAPI_TOKEN=…'}
                value={dotenv}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => {
                  setDotenv(e.target.value);
                  setPreview(null);
                }}
              />
              {!value.id && !global ? (
                <p className="text-xs text-muted">{t('environment.importSavesFirst')}</p>
              ) : null}
              {preview ? (
                <ul className="divide-y divide-border rounded-md border border-border text-sm">
                  {preview.map((v) => (
                    <li key={v.name} className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <code className="min-w-0 flex-1 truncate">{v.name}</code>
                      <span className="text-xs text-muted">
                        {v.operation === 'replace'
                          ? t('environment.importReplace')
                          : t('environment.importAdd')}
                      </span>
                      <select
                        aria-label={`${t('environment.type')} ${v.name}`}
                        className="ui-input"
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
                      {v.secret && !v.secret_allowed ? (
                        <span role="alert" className="w-full text-xs text-status-error">
                          {t('environment.shortSecret')}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy || !dotenv.trim()}
                  className="ui-button ui-button-secondary"
                  onClick={() =>
                    void perform(async () => {
                      const current = global || value.id ? value : await save();
                      setPreview(
                        (
                          await previewEnvironmentImport(
                            global ? null : current.id,
                            current.revision,
                            dotenv,
                          )
                        ).variables,
                      );
                    })
                  }
                >
                  {t('environment.previewImport')}
                </button>
                {preview ? (
                  <button
                    type="button"
                    disabled={busy || preview.some((item) => item.secret && !item.secret_allowed)}
                    className="ui-button ui-button-primary"
                    onClick={() =>
                      void perform(async () => {
                        const current = await ensureSaved();
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
                        setImporting(false);
                      })
                    }
                  >
                    {t('environment.confirmImport', { count: preview.length })}
                  </button>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    ),
    scripts: () => (
      <div className="space-y-5">
        {SCRIPTS.map(([key, label, help]) => (
          <section key={key} className="space-y-2">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <label htmlFor={`${titleId}-${key}`} className="text-sm font-medium text-foreground">
                {t(label)}
              </label>
              {key === 'setup_script' ? (
                <button
                  type="button"
                  className="ui-button ui-button-ghost text-xs"
                  disabled={busy || !value.repo}
                  onClick={() =>
                    void perform(async () => {
                      setSuggestions((await suggestEnvironmentScript(value.repo!)).suggestions);
                    })
                  }
                >
                  {t('environment.suggestScript')}
                </button>
              ) : null}
            </div>
            <p className="text-xs text-muted">{t(help)}</p>
            {key === 'setup_script' && suggestions ? (
              suggestions.length ? (
                <ul className="divide-y divide-border rounded-md border border-border text-sm">
                  {suggestions.map((s) => (
                    <li key={s.file} className="flex flex-wrap items-center gap-2 px-3 py-2">
                      <span className="text-xs text-muted">{s.file}</span>
                      <code className="min-w-0 flex-1 truncate">{s.script}</code>
                      <button
                        type="button"
                        className="ui-button ui-button-secondary text-xs"
                        onClick={() => {
                          change({ ...value, setup_script: s.script });
                          setSuggestions(null);
                        }}
                      >
                        {t('environment.useSuggestion')}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-muted">{t('environment.noSuggestion')}</p>
              )
            ) : null}
            <textarea
              id={`${titleId}-${key}`}
              className="ui-input min-h-24 w-full font-mono text-xs"
              value={value[key] ?? ''}
              maxLength={8000}
              spellCheck={false}
              onChange={(e) => change({ ...value, [key]: e.target.value })}
            />
          </section>
        ))}
        <details className="text-xs text-muted">
          <summary className="cursor-pointer select-none hover:text-foreground">
            {t('environment.platformInfo')}
          </summary>
          <p className="mt-2 leading-5">{t('environment.platformDescription')}</p>
        </details>
      </div>
    ),
    actions: () => (
      <div className="space-y-3">
        <p className="text-sm text-muted">{t('environment.actionsHelp')}</p>
        {value.actions.length === 0 ? (
          <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
            {t('environment.noActions')}
          </p>
        ) : (
          <ol className="space-y-2">
            {value.actions.map((action, index) => (
              <li key={action.id} className="space-y-2 rounded-lg border border-border p-3">
                <div className="flex items-center gap-2">
                  <input
                    aria-label={t('environment.actionName')}
                    className="ui-input min-w-0 flex-1"
                    placeholder={t('environment.actionName')}
                    value={action.name}
                    onChange={(e) =>
                      change({
                        ...value,
                        actions: value.actions.map((a, i) =>
                          i === index ? { ...a, name: e.target.value } : a,
                        ),
                      })
                    }
                  />
                  <button
                    type="button"
                    className="ui-icon-button ui-button-ghost"
                    aria-label={t('environment.moveUp')}
                    disabled={index === 0}
                    onClick={() => moveAction(index, -1)}
                  >
                    <ArrowIcon up />
                  </button>
                  <button
                    type="button"
                    className="ui-icon-button ui-button-ghost"
                    aria-label={t('environment.moveDown')}
                    disabled={index === value.actions.length - 1}
                    onClick={() => moveAction(index, 1)}
                  >
                    <ArrowIcon up={false} />
                  </button>
                  <button
                    type="button"
                    className="ui-icon-button ui-button-ghost hover:!text-status-error"
                    aria-label={t('environment.removeAction')}
                    onClick={() =>
                      change({
                        ...value,
                        actions: value.actions.filter((a) => a.id !== action.id),
                      })
                    }
                  >
                    <RemoveIcon />
                  </button>
                </div>
                <textarea
                  aria-label={t('environment.command')}
                  className="ui-input min-h-16 w-full font-mono text-xs"
                  placeholder="npm test"
                  value={action.command}
                  spellCheck={false}
                  onChange={(e) =>
                    change({
                      ...value,
                      actions: value.actions.map((a, i) =>
                        i === index ? { ...a, command: e.target.value } : a,
                      ),
                    })
                  }
                />
                <label className="flex items-center gap-2 text-xs text-muted">
                  {t('environment.directory')}
                  <input
                    aria-label={t('environment.directory')}
                    className="ui-input min-w-0 flex-1 font-mono text-xs"
                    value={action.cwd}
                    spellCheck={false}
                    onChange={(e) =>
                      change({
                        ...value,
                        actions: value.actions.map((a, i) =>
                          i === index ? { ...a, cwd: e.target.value } : a,
                        ),
                      })
                    }
                  />
                </label>
              </li>
            ))}
          </ol>
        )}
        <button
          type="button"
          className="ui-button ui-button-secondary"
          onClick={() =>
            change({
              ...value,
              actions: [
                ...value.actions,
                { id: crypto.randomUUID(), name: '', command: '', cwd: '.' },
              ],
            })
          }
        >
          <span aria-hidden="true">+</span>
          {t('environment.addAction')}
        </button>
      </div>
    ),
    test: () => (
      <div className="space-y-4">
        <p className="text-sm text-muted">{t('environment.testHelp')}</p>
        {run ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <StateBadge state={run.state} />
              {run.base_sha ? (
                <span className="font-mono text-xs text-muted">
                  {t('environment.testedCommit', { sha: run.base_sha.slice(0, 8) })}
                </span>
              ) : null}
              {testing ? (
                <button
                  type="button"
                  className="ui-button ui-button-ghost ml-auto"
                  onClick={() =>
                    void perform(async () => setRun(await cancelEnvironmentRun(run.id)))
                  }
                >
                  {t('environment.cancelTest')}
                </button>
              ) : null}
            </div>
            {testIsOlder ? (
              <p className="text-xs text-status-warning">{t('environment.olderTest')}</p>
            ) : null}
            {run.detail ? <p className="text-sm text-status-error">{run.detail}</p> : null}
            <EnvironmentSteps steps={run.steps} />
            {snapshotsEnabled && run.state === 'succeeded' && value.setup_script ? (
              <p className="text-xs text-muted">{t('environment.snapshot.savedByTest')}</p>
            ) : null}
          </div>
        ) : (
          <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
            {t('environment.noTest')}
          </p>
        )}
      </div>
    ),
    history: () =>
      !history ? (
        <p role="status" className="text-sm text-muted">
          {t('environment.loading')}
        </p>
      ) : history.length === 0 ? (
        <p className="text-sm text-muted">{t('environment.historyEmpty')}</p>
      ) : (
        <ol className="space-y-2">
          {history.map((version) => (
            <li key={version.revision} className="rounded-lg border border-border">
              <details>
                <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm hover:bg-surface-hover">
                  <span className="font-medium">
                    {t('environment.versionLabel', { revision: version.revision })}
                  </span>
                  {version.revision === value.revision ? (
                    <span className="rounded-full bg-accent-subtle px-2 py-0.5 text-xs text-accent">
                      {t('environment.currentVersion')}
                    </span>
                  ) : null}
                  <span className="text-xs text-muted">
                    {t('environment.variableCount', { count: version.variables.length })}
                  </span>
                  {version.created_at ? (
                    <time dateTime={version.created_at} className="ml-auto text-xs text-muted">
                      {new Date(version.created_at).toLocaleString()}
                    </time>
                  ) : null}
                </summary>
                <div className="space-y-3 border-t border-border px-3 py-3 text-sm">
                  {version.variables.length ? (
                    <ul className="space-y-1 text-xs">
                      {version.variables.map((v) => (
                        <li key={v.name} className="flex gap-2">
                          <code className="text-foreground">{v.name}</code>
                          <span className="truncate text-muted">
                            {v.secret ? t('environment.secret') : v.value}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {SCRIPTS.map(([key, label]) =>
                    version[key] ? (
                      <div key={key}>
                        <p className="mb-1 text-xs text-muted">{t(label)}</p>
                        <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-surface-muted p-2 font-mono text-xs">
                          {version[key]}
                        </pre>
                      </div>
                    ) : null,
                  )}
                  {version.actions.length ? (
                    <p className="text-xs text-muted">
                      {t('environment.tab.actions')}:{' '}
                      {version.actions.map((a) => a.name).join(', ')}
                    </p>
                  ) : null}
                  {version.revision === value.revision ? null : restoring === version.revision ? (
                    <div className="space-y-2 rounded-md bg-surface-muted p-2">
                      <p className="text-xs">
                        {t('environment.restoreConfirm', { revision: value.revision + 1 })}
                      </p>
                      <div className="flex gap-2">
                        <button
                          type="button"
                          className="ui-button ui-button-primary"
                          disabled={busy}
                          onClick={() =>
                            void perform(async () => {
                              adopt(
                                await restoreEnvironment(
                                  value.id!,
                                  value.revision,
                                  version.revision,
                                  global,
                                ),
                              );
                              setRestoring(null);
                            })
                          }
                        >
                          {t('environment.confirmRestore')}
                        </button>
                        <button
                          type="button"
                          className="ui-button ui-button-ghost"
                          onClick={() => setRestoring(null)}
                        >
                          {t('environment.cancel')}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="ui-button ui-button-secondary"
                      onClick={() => setRestoring(version.revision)}
                    >
                      {t('environment.restore')}
                    </button>
                  )}
                </div>
              </details>
            </li>
          ))}
        </ol>
      ),
  };

  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('environment.close')}
        onClick={requestClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`relative flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card ${
          // A fixed height, so moving between sections does not resize the dialog.
          tabs.length > 1 ? 'h-[min(46rem,100%)]' : ''
        }`}
      >
        <div className="shrink-0 space-y-3 px-6 pb-3 pt-5">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h2 id={titleId} className="truncate text-lg font-semibold text-foreground">
                {global
                  ? t('environment.globalVariables')
                  : isNew
                    ? t('environment.new')
                    : value.name}
              </h2>
              <p className="mt-0.5 truncate text-sm text-muted">
                {global ? t('environment.globalDescription') : value.repo}
                {!isNew ? ` · ${t('environment.versionLabel', { revision: value.revision })}` : ''}
              </p>
            </div>
            <button
              type="button"
              className="ui-icon-button ui-button-ghost shrink-0"
              aria-label={t('environment.close')}
              disabled={busy}
              onClick={requestClose}
            >
              <RemoveIcon />
            </button>
          </div>
          {global ? null : (
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="space-y-1 text-sm">
                <span className="text-muted">{t('environment.name')}</span>
                <input
                  data-environment-name
                  className="ui-input w-full"
                  placeholder={t('environment.namePlaceholder')}
                  value={value.name}
                  maxLength={80}
                  onChange={(e) => change({ ...value, name: e.target.value })}
                />
              </label>
              {isNew && !initial.repo ? (
                <label className="space-y-1 text-sm">
                  <span className="text-muted">{t('environment.repository')}</span>
                  <select
                    className="ui-input w-full"
                    value={value.repo ?? ''}
                    onChange={(e) => change({ ...value, repo: e.target.value })}
                  >
                    <option value="">{t('environment.chooseRepository')}</option>
                    {repos.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </div>
          )}
          {tabs.length > 1 ? (
            <div
              role="tablist"
              aria-label={t('environment.tabs')}
              className="flex gap-1 overflow-x-auto rounded-lg border border-border bg-surface-muted p-1"
            >
              {tabs.map((id, index) => (
                <button
                  key={id}
                  ref={(element) => {
                    tabButtons.current[id] = element;
                  }}
                  type="button"
                  role="tab"
                  id={`${titleId}-tab-${id}`}
                  aria-controls={`${titleId}-panel`}
                  aria-selected={tab === id}
                  tabIndex={tab === id ? 0 : -1}
                  onClick={() => setTab(id)}
                  onKeyDown={(event) => {
                    const next: Partial<Record<string, number>> = {
                      ArrowRight: (index + 1) % tabs.length,
                      ArrowLeft: (index - 1 + tabs.length) % tabs.length,
                      Home: 0,
                      End: tabs.length - 1,
                    };
                    const target = next[event.key];
                    if (target === undefined) return;
                    event.preventDefault();
                    setTab(tabs[target]);
                    tabButtons.current[tabs[target]]?.focus();
                  }}
                  className={`ui-focus-ring min-h-8 shrink-0 rounded-md px-3 text-sm font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
                    tab === id
                      ? 'bg-surface text-foreground shadow-subtle'
                      : 'text-muted hover:bg-surface-hover hover:text-foreground'
                  }`}
                >
                  {t(TAB_LABELS[id])}
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <div
          id={`${titleId}-panel`}
          role={tabs.length > 1 ? 'tabpanel' : undefined}
          aria-labelledby={tabs.length > 1 ? `${titleId}-tab-${tab}` : undefined}
          className="min-h-0 flex-1 overflow-y-auto px-6 pb-5 pt-2"
        >
          {tabPanels[tab]()}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-6 py-3">
          {confirmingClose ? (
            <>
              <p className="mr-auto text-sm text-foreground">{t('environment.discardConfirm')}</p>
              <button
                type="button"
                className="ui-button ui-button-secondary"
                onClick={() => setConfirmingClose(false)}
              >
                {t('environment.keepEditing')}
              </button>
              <button type="button" className="ui-button ui-button-primary" onClick={onClose}>
                {t('environment.discard')}
              </button>
            </>
          ) : (
            <>
              <p
                role={error ? 'alert' : 'status'}
                className={`mr-auto min-w-0 whitespace-pre-wrap text-sm ${error ? 'text-status-error' : 'text-muted'}`}
              >
                {error ||
                  (dirty
                    ? t('environment.unsaved')
                    : saved
                      ? t(global ? 'environment.savedGlobal' : 'environment.savedForNew')
                      : '')}
              </p>
              {global ? null : (
                <button
                  type="button"
                  disabled={busy || testing}
                  className="ui-button ui-button-secondary"
                  onClick={() => void saveAndTest()}
                >
                  {t(dirty || isNew ? 'environment.saveAndTest' : 'environment.runTest')}
                </button>
              )}
              <button
                type="button"
                disabled={busy || (!dirty && !isNew)}
                className="ui-button ui-button-primary"
                onClick={() => void perform(save)}
              >
                {t('environment.save')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
