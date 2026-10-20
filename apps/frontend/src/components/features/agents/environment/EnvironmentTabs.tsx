'use client';

import type { ReactNode } from 'react';
import { useT } from '@/components/providers';
import type {
  EnvironmentAction,
  EnvironmentConfig,
  EnvironmentRun,
  EnvironmentTeam,
  EnvironmentVariable,
  VariableEdit,
} from '@/lib/api/environments';
import { EnvironmentSteps, StateBadge } from './EnvironmentStatus';
import { RemoveIcon, VariableEditor } from './VariableEditor';

// One component per tab of the environment dialog. The dialog keeps every
// piece of state, because a tab is unmounted when another is chosen and a
// draft (an import, a restore being confirmed) must survive that; the tabs
// draw it and report what the owner did.

export const SCRIPTS = [
  ['setup_script', 'environment.installScript', 'environment.installHelp'],
  ['update_script', 'environment.updateScript', 'environment.updateHelp'],
  ['start_script', 'environment.startScript', 'environment.startHelp'],
  ['verify_script', 'environment.verifyScript', 'environment.verifyHelp'],
] as const;

/** One variable a `.env` import would change, and how it would store it. */
export interface ImportPreviewItem {
  name: string;
  operation: string;
  secret: boolean;
  secret_allowed: boolean;
}

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

export function VariablesTab({
  variables,
  onVariablesChange,
  inherited,
  team,
  children,
}: {
  variables: VariableEdit[];
  onVariablesChange: (next: VariableEdit[]) => void;
  /** The owner's globals, shown beside a personal environment's own. */
  inherited: EnvironmentVariable[];
  team: EnvironmentTeam | null;
  /** The `.env` import, below the editor. */
  children: ReactNode;
}) {
  const t = useT();
  return (
    <div className="space-y-4">
      <VariableEditor values={variables} onChange={onVariablesChange} inherited={inherited} />
      {team ? (
        <div className="space-y-1 rounded-lg border border-status-warning/40 bg-status-warning/5 px-3 py-2 text-xs leading-5">
          <p className="text-foreground">{t('environment.teamSecrets', { team: team.name })}</p>
          <p className="text-muted">{t('environment.teamGlobals')}</p>
        </div>
      ) : null}
      {children}
    </div>
  );
}

export function DotenvImport({
  open,
  onToggle,
  dotenv,
  onDotenvChange,
  savesFirst,
  preview,
  onPreviewChange,
  busy,
  onPreview,
  onImport,
}: {
  open: boolean;
  onToggle: () => void;
  dotenv: string;
  onDotenvChange: (dotenv: string) => void;
  /** The environment has not been saved, so previewing saves it first. */
  savesFirst: boolean;
  preview: ImportPreviewItem[] | null;
  onPreviewChange: (preview: ImportPreviewItem[]) => void;
  busy: boolean;
  onPreview: () => void;
  onImport: (preview: ImportPreviewItem[]) => void;
}) {
  const t = useT();
  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium hover:bg-surface-hover"
      >
        {t('environment.importEnv')}
        <ArrowIcon up={open} />
      </button>
      {open ? (
        <div className="space-y-3 border-t border-border p-3">
          <p className="text-xs text-muted">{t('environment.importHint')}</p>
          <textarea
            aria-label={t('environment.dotenv')}
            className="ui-input min-h-28 w-full font-mono text-xs"
            placeholder={'API_URL=https://example.com\nAPI_TOKEN=…'}
            value={dotenv}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => onDotenvChange(e.target.value)}
          />
          {savesFirst ? (
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
                      onPreviewChange(
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
              onClick={onPreview}
            >
              {t('environment.previewImport')}
            </button>
            {preview ? (
              <button
                type="button"
                disabled={busy || preview.some((item) => item.secret && !item.secret_allowed)}
                className="ui-button ui-button-primary"
                onClick={() => onImport(preview)}
              >
                {t('environment.confirmImport', { count: preview.length })}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function ScriptsTab({
  value,
  onChange,
  idPrefix,
  busy,
  suggestions,
  onSuggest,
  onUseSuggestion,
}: {
  value: EnvironmentConfig;
  onChange: (next: EnvironmentConfig) => void;
  /** Ties each script's label to its box. */
  idPrefix: string;
  busy: boolean;
  /** Install scripts found from the repository's lockfiles, once asked for. */
  suggestions: { file: string; script: string }[] | null;
  onSuggest: () => void;
  onUseSuggestion: (script: string) => void;
}) {
  const t = useT();
  return (
    <div className="space-y-5">
      {SCRIPTS.map(([key, label, help]) => (
        <section key={key} className="space-y-2">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <label htmlFor={`${idPrefix}-${key}`} className="text-sm font-medium text-foreground">
              {t(label)}
            </label>
            {key === 'setup_script' ? (
              <button
                type="button"
                className="ui-button ui-button-ghost text-xs"
                disabled={busy || !value.repo}
                onClick={onSuggest}
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
                      onClick={() => onUseSuggestion(s.script)}
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
            id={`${idPrefix}-${key}`}
            className="ui-input min-h-24 w-full font-mono text-xs"
            value={value[key] ?? ''}
            maxLength={8000}
            spellCheck={false}
            onChange={(e) => onChange({ ...value, [key]: e.target.value })}
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
  );
}

export function ActionsTab({
  actions,
  onChange,
}: {
  actions: EnvironmentAction[];
  onChange: (actions: EnvironmentAction[]) => void;
}) {
  const t = useT();
  const edit = (index: number, changes: Partial<EnvironmentAction>) =>
    onChange(actions.map((a, i) => (i === index ? { ...a, ...changes } : a)));
  const move = (index: number, by: number) => {
    const next = [...actions];
    [next[index], next[index + by]] = [next[index + by], next[index]];
    onChange(next);
  };
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">{t('environment.actionsHelp')}</p>
      {actions.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
          {t('environment.noActions')}
        </p>
      ) : (
        <ol className="space-y-2">
          {actions.map((action, index) => (
            <li key={action.id} className="space-y-2 rounded-lg border border-border p-3">
              <div className="flex items-center gap-2">
                <input
                  aria-label={t('environment.actionName')}
                  className="ui-input min-w-0 flex-1"
                  placeholder={t('environment.actionName')}
                  value={action.name}
                  onChange={(e) => edit(index, { name: e.target.value })}
                />
                <button
                  type="button"
                  className="ui-icon-button ui-button-ghost"
                  aria-label={t('environment.moveUp')}
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                >
                  <ArrowIcon up />
                </button>
                <button
                  type="button"
                  className="ui-icon-button ui-button-ghost"
                  aria-label={t('environment.moveDown')}
                  disabled={index === actions.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <ArrowIcon up={false} />
                </button>
                <button
                  type="button"
                  className="ui-icon-button ui-button-ghost hover:!text-status-error"
                  aria-label={t('environment.removeAction')}
                  onClick={() => onChange(actions.filter((a) => a.id !== action.id))}
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
                onChange={(e) => edit(index, { command: e.target.value })}
              />
              <label className="flex items-center gap-2 text-xs text-muted">
                {t('environment.directory')}
                <input
                  aria-label={t('environment.directory')}
                  className="ui-input min-w-0 flex-1 font-mono text-xs"
                  value={action.cwd}
                  spellCheck={false}
                  onChange={(e) => edit(index, { cwd: e.target.value })}
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
          onChange([...actions, { id: crypto.randomUUID(), name: '', command: '', cwd: '.' }])
        }
      >
        <span aria-hidden="true">+</span>
        {t('environment.addAction')}
      </button>
    </div>
  );
}

export function TestTab({
  run,
  testing,
  testIsOlder,
  snapshotNote,
  onCancel,
}: {
  run: EnvironmentRun | null;
  testing: boolean;
  /** The environment changed since this test ran. */
  testIsOlder: boolean;
  /** A passing test also saved the installation for new tasks. */
  snapshotNote: boolean;
  onCancel: (run: EnvironmentRun) => void;
}) {
  const t = useT();
  return (
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
                onClick={() => onCancel(run)}
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
          {snapshotNote && run.state === 'succeeded' ? (
            <p className="text-xs text-muted">{t('environment.snapshot.prebuildAfterTest')}</p>
          ) : null}
        </div>
      ) : (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">
          {t('environment.noTest')}
        </p>
      )}
    </div>
  );
}

export function HistoryTab({
  history,
  currentRevision,
  busy,
  restoring,
  onRestoring,
  onRestore,
}: {
  history: EnvironmentConfig[] | null;
  currentRevision: number;
  busy: boolean;
  /** The version whose restore is waiting to be confirmed. */
  restoring: number | null;
  onRestoring: (revision: number | null) => void;
  onRestore: (revision: number) => void;
}) {
  const t = useT();
  if (!history) {
    return (
      <p role="status" className="text-sm text-muted">
        {t('environment.loading')}
      </p>
    );
  }
  if (history.length === 0) {
    return <p className="text-sm text-muted">{t('environment.historyEmpty')}</p>;
  }
  return (
    <ol className="space-y-2">
      {history.map((version) => (
        <li key={version.revision} className="rounded-lg border border-border">
          <details>
            <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm hover:bg-surface-hover">
              <span className="font-medium">
                {t('environment.versionLabel', { revision: version.revision })}
              </span>
              {version.revision === currentRevision ? (
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
                  {t('environment.tab.actions')}: {version.actions.map((a) => a.name).join(', ')}
                </p>
              ) : null}
              {version.revision === currentRevision ? null : restoring === version.revision ? (
                <div className="space-y-2 rounded-md bg-surface-muted p-2">
                  <p className="text-xs">
                    {t('environment.restoreConfirm', { revision: currentRevision + 1 })}
                  </p>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      className="ui-button ui-button-primary"
                      disabled={busy}
                      onClick={() => onRestore(version.revision)}
                    >
                      {t('environment.confirmRestore')}
                    </button>
                    <button
                      type="button"
                      className="ui-button ui-button-ghost"
                      onClick={() => onRestoring(null)}
                    >
                      {t('environment.cancel')}
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  type="button"
                  className="ui-button ui-button-secondary"
                  onClick={() => onRestoring(version.revision)}
                >
                  {t('environment.restore')}
                </button>
              )}
            </div>
          </details>
        </li>
      ))}
    </ol>
  );
}
