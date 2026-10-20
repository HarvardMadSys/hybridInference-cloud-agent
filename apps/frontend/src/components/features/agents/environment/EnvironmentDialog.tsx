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
import type {
  EnvironmentConfig,
  EnvironmentRun,
  EnvironmentTeam,
  VariableEdit,
} from '@/lib/api/environments';
import type { TranslationKey } from '@/lib/i18n/translate';
import { ACTIVE_STATES } from './EnvironmentStatus';
import {
  ActionsTab,
  DotenvImport,
  HistoryTab,
  type ImportPreviewItem,
  ScriptsTab,
  TestTab,
  VariablesTab,
} from './EnvironmentTabs';
import { RemoveIcon, variableEdits } from './VariableEditor';

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

const message = (cause: unknown) => (cause instanceof Error ? cause.message : String(cause));

/** Whose a new environment or copy is: yours, or a team's you own or administer. */
export function OwnerSelect({
  label,
  teams,
  value,
  onChange,
}: {
  label: string;
  teams: EnvironmentTeam[];
  /** A team's id, or empty for your own. */
  value: string;
  onChange: (teamId: string) => void;
}) {
  const t = useT();
  return (
    <label className="space-y-1 text-sm">
      <span className="text-muted">{label}</span>
      <select className="ui-input w-full" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('environment.ownerSelf')}</option>
        {teams.map((team) => (
          <option key={team.id} value={team.id}>
            {t('environment.ownerTeam', { team: team.name })}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * Edit one environment, or the owner's global variables when `initial.repo`
 * is null. A new environment whose repository is still to be chosen has an
 * empty `repo` and is given `repos` to choose from, and is created for one of
 * `teams` when one is chosen. A team environment you may not change opens
 * read-only: it can still be tested, and its history read.
 */
export function EnvironmentDialog({
  initial,
  globals,
  repos = [],
  teams = [],
  snapshotsEnabled = false,
  onSaved,
  onClose,
}: {
  initial: EnvironmentConfig;
  globals: EnvironmentConfig;
  repos?: string[];
  /** Teams a new environment may be created for: those you own or administer. */
  teams?: EnvironmentTeam[];
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
  const [preview, setPreview] = useState<ImportPreviewItem[] | null>(null);
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
  const team = global ? null : (value.team ?? null);
  const readOnly = !global && value.can_edit === false;
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

  const tabPanels: Record<Tab, () => ReactNode> = {
    variables: () => (
      <VariablesTab
        variables={variables}
        onVariablesChange={(next) => {
          setVariables(next);
          setDirty(true);
          setSaved(false);
        }}
        // A team environment uses each member's own globals only at run
        // time, so yours are not shown as part of it.
        inherited={global || team ? [] : globals.variables}
        team={team}
      >
        <DotenvImport
          open={importing}
          onToggle={() => setImporting(!importing)}
          dotenv={dotenv}
          onDotenvChange={(next) => {
            setDotenv(next);
            setPreview(null);
          }}
          savesFirst={!value.id && !global}
          preview={preview}
          onPreviewChange={setPreview}
          busy={busy}
          onPreview={() =>
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
          onImport={(confirmed) =>
            void perform(async () => {
              const current = await ensureSaved();
              adopt(
                await importEnvironmentVariables(
                  global ? null : current.id,
                  current.revision,
                  global ? current.revision : globals.revision,
                  dotenv,
                  confirmed.filter((item) => !item.secret).map((item) => item.name),
                ),
              );
              setDotenv('');
              setPreview(null);
              setImporting(false);
            })
          }
        />
      </VariablesTab>
    ),
    scripts: () => (
      <ScriptsTab
        value={value}
        onChange={change}
        idPrefix={titleId}
        busy={busy}
        suggestions={suggestions}
        onSuggest={() =>
          void perform(async () => {
            setSuggestions((await suggestEnvironmentScript(value.repo!)).suggestions);
          })
        }
        onUseSuggestion={(script) => {
          change({ ...value, setup_script: script });
          setSuggestions(null);
        }}
      />
    ),
    actions: () => (
      <ActionsTab actions={value.actions} onChange={(actions) => change({ ...value, actions })} />
    ),
    test: () => (
      <TestTab
        run={run}
        testing={testing}
        testIsOlder={testIsOlder}
        snapshotNote={snapshotsEnabled && Boolean(value.setup_script)}
        onCancel={(current) =>
          void perform(async () => setRun(await cancelEnvironmentRun(current.id)))
        }
      />
    ),
    history: () => (
      <HistoryTab
        history={history}
        currentRevision={value.revision}
        busy={busy}
        restoring={restoring}
        onRestoring={setRestoring}
        onRestore={(revision) =>
          void perform(async () => {
            adopt(await restoreEnvironment(value.id!, value.revision, revision, global));
            setRestoring(null);
          })
        }
      />
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
                  disabled={readOnly}
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
              {isNew && teams.length ? (
                <OwnerSelect
                  label={t('environment.owner')}
                  teams={teams}
                  value={value.team?.id ?? ''}
                  onChange={(id) =>
                    change({ ...value, team: teams.find((item) => item.id === id) ?? null })
                  }
                />
              ) : null}
            </div>
          )}
          {team ? (
            <p className="rounded-lg bg-surface-muted px-3 py-2 text-xs leading-5 text-muted">
              {t(
                readOnly
                  ? 'environment.teamReadOnly'
                  : isNew
                    ? 'environment.teamNew'
                    : 'environment.teamShared',
                { team: team.name },
              )}
            </p>
          ) : null}
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
          {/* Read-only disables every control but a test's own. */}
          <fieldset disabled={readOnly && tab !== 'test'} className="m-0 min-w-0 border-0 p-0">
            {tabPanels[tab]()}
          </fieldset>
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
              {readOnly ? null : (
                <button
                  type="button"
                  disabled={busy || (!dirty && !isNew)}
                  className="ui-button ui-button-primary"
                  onClick={() => void perform(save)}
                >
                  {t('environment.save')}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
