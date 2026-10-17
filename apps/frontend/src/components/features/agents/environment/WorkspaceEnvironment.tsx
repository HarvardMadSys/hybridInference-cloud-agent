'use client';

import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  getEnvironmentActionRun,
  getGlobalEnvironment,
  getWorkspaceEnvironment,
  listEnvironments,
  refreshWorkspaceRuntime,
  skipWorkspaceStart,
  retryWorkspaceEnvironment,
  startEnvironmentAction,
  stopEnvironmentAction,
} from '@/lib/api/environments';
import type {
  ActionRun,
  EnvironmentBinding,
  EnvironmentConfig,
  EnvironmentVariable,
} from '@/lib/api/environments';
import type { TranslationKey } from '@/lib/i18n/translate';
import { EnvironmentDialog } from './EnvironmentDialog';
import { emptyEnvironment } from './EnvironmentManager';
import {
  ACTIVE_STATES,
  Badge,
  EnvironmentSteps,
  StateBadge,
  TeamBadge,
  usePhaseLabel,
} from './EnvironmentStatus';

type Status = 'none' | 'pending' | 'preparing' | 'ready' | 'failed';
const STATUS: Record<
  Status,
  { key: TranslationKey; tone: 'success' | 'error' | 'progress' | 'neutral' }
> = {
  none: { key: 'environment.status.none', tone: 'neutral' },
  pending: { key: 'environment.status.pending', tone: 'neutral' },
  preparing: { key: 'environment.status.preparing', tone: 'progress' },
  ready: { key: 'environment.status.ready', tone: 'success' },
  failed: { key: 'environment.status.failed', tone: 'error' },
};

function status(bound: EnvironmentBinding): Status {
  if (bound.steps.some((s) => s.state === 'failed')) return 'failed';
  if (bound.steps.some((s) => s.state === 'running')) return 'preparing';
  if (bound.steps.length) return 'ready';
  return bound.required ? 'pending' : 'none';
}

function VariableList({
  variables,
  sourceLabel,
}: {
  variables: EnvironmentVariable[];
  sourceLabel: (source: EnvironmentVariable['source']) => string;
}) {
  const t = useT();
  if (!variables.length)
    return <p className="text-xs text-muted">{t('environment.noVariables')}</p>;
  return (
    <ul className="divide-y divide-border rounded-lg border border-border text-xs">
      {variables.map((v) => (
        <li key={v.name} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-3 py-1.5">
          <code className="min-w-0 truncate text-foreground">{v.name}</code>
          <span className="min-w-0 flex-1 truncate font-mono text-muted">
            {v.secret ? '••••••' : v.value}
          </span>
          {v.phase !== 'both' ? (
            <span className="text-muted">
              {t(v.phase === 'setup' ? 'environment.setupOnly' : 'environment.runtimeOnly')}
            </span>
          ) : null}
          <span className="text-muted">{sourceLabel(v.source)}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The idempotency key of each action start whose answer has not arrived, by
 * conversation turn and action. Outside the panel, which unmounts whenever
 * another tab is chosen: a Run pressed again after coming back is the same
 * request, not a second copy of the command.
 */
const pendingStarts = new Map<string, string>();

/**
 * A conversation's Environment panel. What the reader needs first is whether
 * the workspace is ready and, when it is not, what to do about it; then the
 * saved commands they can run. The fixed configuration behind it is detail.
 */
export function WorkspaceEnvironment({
  jobId,
  repo,
  onTerminal,
}: {
  jobId: string;
  repo: string;
  onTerminal: () => void;
}) {
  const t = useT();
  const phase = usePhaseLabel();
  const temporary = isTemporaryWorkspace(repo);
  const [bound, setBound] = useState<EnvironmentBinding | null>(null);
  const [error, setError] = useState('');
  // A failed read of the workspace's environment lasts until the next read
  // succeeds; an action's failure stays until the owner acts again.
  const [loadError, setLoadError] = useState('');
  const [runs, setRuns] = useState<Record<string, ActionRun>>({});
  // The actions whose listed run this panel has already taken in.
  const listedRuns = useRef(new Set<string>());
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [skipArmed, setSkipArmed] = useState(false);
  const [editing, setEditing] = useState<{
    config: EnvironmentConfig;
    globals: EnvironmentConfig;
  } | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void getWorkspaceEnvironment(jobId)
        .then((value) => {
          if (!active) return;
          setBound(value);
          setLoadError('');
          adoptRuns(value.action_runs ?? []);
        })
        .catch((e) => {
          if (active) setLoadError(e instanceof Error ? e.message : String(e));
        });
    };
    // The panel's own record of a run is newer than any listing: it may hold a
    // start this read predates. A listed run fills only an action the panel
    // knows nothing about, as when it was opened again mid-run; a finished one
    // is read once more for the output the listing leaves out.
    const adoptRuns = (listed: ActionRun[]) => {
      const known = listedRuns.current;
      const adopted = listed.filter((run) => run.action_id && !known.has(run.action_id));
      if (!adopted.length) return;
      adopted.forEach((run) => known.add(run.action_id!));
      setRuns((previous) => {
        const next = { ...previous };
        for (const run of adopted) next[run.action_id!] ??= run;
        return next;
      });
      adopted
        .filter((run) => !ACTIVE_STATES.includes(run.state))
        .forEach((run) => {
          void getEnvironmentActionRun(run.id)
            .then((value) => {
              if (!active) return;
              setRuns((previous) =>
                previous[run.action_id!]?.id === run.id
                  ? { ...previous, [run.action_id!]: value }
                  : previous,
              );
            })
            .catch(() => {
              // The state is already shown; only its output is missing.
            });
        });
    };
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [jobId, reload]);
  useEffect(() => {
    let active = true;
    const current = Object.entries(runs).filter(([, run]) => ACTIVE_STATES.includes(run.state));
    if (!current.length) return;
    const timer = setInterval(() => {
      current.forEach(([id, run]) => {
        void getEnvironmentActionRun(run.id)
          .then((value) => {
            if (active) setRuns((previous) => ({ ...previous, [id]: value }));
          })
          .catch((e) => {
            if (active) setError(e instanceof Error ? e.message : String(e));
          });
      });
    }, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [runs]);
  async function perform(action: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  // The saved environment as it is now, which may be newer than the version
  // this workspace is fixed to; editing it changes what new tasks get.
  const openEditor = () =>
    perform(async () => {
      if (temporary) {
        const globals = await getGlobalEnvironment();
        setEditing({ config: globals, globals });
        return;
      }
      const result = await listEnvironments(repo);
      const saved = result.environments.find((e) => e.id === bound?.environment_id);
      setEditing({ config: saved ?? emptyEnvironment(repo), globals: result.globals });
    });

  if (!bound) {
    return (
      <section className="p-4" aria-label={t('environment.workspaceTitle')}>
        {error || loadError ? (
          <p role="alert" className="text-sm text-status-error">
            {error || loadError}
          </p>
        ) : (
          <p role="status" className="text-sm text-muted">
            {t('environment.loading')}
          </p>
        )}
      </section>
    );
  }
  const state = status(bound);
  const failed = bound.steps.find((s) => s.state === 'failed');
  const name = bound.legacy
    ? t('environment.legacy')
    : (bound.environment_name ?? t('environment.none'));
  const sourceLabel = (source: EnvironmentVariable['source']) =>
    source === 'global'
      ? t('environment.sourceGlobal')
      : source === 'override'
        ? t('environment.sourceTask')
        : (bound.environment_name ?? '');
  // Whether you may change a team's environment is known once it is opened.
  const editLabel: TranslationKey = temporary
    ? 'environment.editGlobals'
    : bound.environment_id && !bound.environment_archived
      ? bound.team
        ? 'environment.openTeamSaved'
        : 'environment.editSaved'
      : 'environment.createForRepository';
  return (
    <section className="space-y-5 p-4" aria-label={t('environment.workspaceTitle')}>
      <div className="space-y-3 rounded-lg border border-border p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-foreground">{name}</h2>
            {bound.environment_id ? (
              <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                {t('environment.versionLabel', { revision: bound.revision ?? 0 })}
                {bound.team ? <TeamBadge name={bound.team.name} /> : null}
              </p>
            ) : null}
          </div>
          <Badge tone={STATUS[state].tone}>{t(STATUS[state].key)}</Badge>
        </div>
        {state === 'pending' ? (
          <p className="text-xs text-muted">{t('environment.pendingHint')}</p>
        ) : null}
        <EnvironmentSteps steps={bound.steps} />
        {failed ? (
          <div className="space-y-2 rounded-md bg-status-error/5 p-2">
            <p className="text-sm font-medium text-status-error">
              {t('environment.failedStep', { phase: phase(failed.phase) })}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="ui-button ui-button-secondary"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    await retryWorkspaceEnvironment(jobId);
                    setReload((v) => v + 1);
                  })
                }
              >
                {t('environment.retryPreparation')}
              </button>
              {failed.phase === 'start' ? (
                <button
                  type="button"
                  className="ui-button ui-button-ghost"
                  disabled={busy || skipArmed}
                  onClick={() =>
                    void perform(async () => {
                      await skipWorkspaceStart(jobId);
                      setSkipArmed(true);
                    })
                  }
                >
                  {t('environment.skipStart')}
                </button>
              ) : null}
            </div>
            {failed.phase === 'start' ? (
              <p className="text-xs text-muted">
                {t(skipArmed ? 'environment.skipArmed' : 'environment.skipHint')}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>

      {bound.environment_archived ? (
        <p className="rounded-lg border border-border bg-surface-muted p-3 text-xs text-muted">
          {t('environment.archived')}
        </p>
      ) : bound.has_new_version ? (
        <div className="space-y-2 rounded-lg border border-status-info/30 bg-status-info/5 p-3 text-xs">
          <p className="text-foreground">
            {t('environment.newVersionAvailable', { revision: bound.revision ?? 0 })}
          </p>
          <a
            href={`/?repo=${encodeURIComponent(repo)}`}
            className="inline-block font-medium text-accent hover:underline"
          >
            {t('environment.newWorkspace')}
          </a>
        </div>
      ) : null}

      {bound.actions.length ? (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-foreground">{t('environment.actions')}</h3>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {bound.actions.map((action) => {
              const run = runs[action.id];
              const running = !!run && ACTIVE_STATES.includes(run.state);
              return (
                <li key={action.id} className="space-y-2 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                      {action.name}
                    </span>
                    {run ? <StateBadge state={run.state} /> : null}
                    {running ? (
                      <button
                        type="button"
                        className="ui-button ui-button-secondary"
                        disabled={busy}
                        onClick={() =>
                          void perform(async () => {
                            const result = await stopEnvironmentAction(run.id);
                            setRuns((v) => ({ ...v, [action.id]: result }));
                          })
                        }
                      >
                        {t('environment.stop')}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="ui-button ui-button-secondary"
                        disabled={busy}
                        onClick={() =>
                          void perform(async () => {
                            // The same key for a retry after a lost response, so
                            // the command is not started twice.
                            const pending = `${jobId}:${action.id}`;
                            const key = pendingStarts.get(pending) ?? crypto.randomUUID();
                            pendingStarts.set(pending, key);
                            const result = await startEnvironmentAction(jobId, action.id, key);
                            pendingStarts.delete(pending);
                            setRuns((v) => ({ ...v, [action.id]: result }));
                          })
                        }
                      >
                        {t('environment.run')}
                      </button>
                    )}
                    <button
                      type="button"
                      className="ui-button ui-button-ghost"
                      onClick={onTerminal}
                    >
                      {t('environment.openOutput')}
                    </button>
                  </div>
                  <code className="block truncate font-mono text-xs text-muted">
                    {action.command}
                  </code>
                  {run?.output ? (
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md bg-surface-muted p-2 font-mono text-xs">
                      {run.output}
                    </pre>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      <details className="rounded-lg border border-border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium hover:bg-surface-hover">
          {t('environment.configuration')}
        </summary>
        <div className="space-y-4 border-t border-border p-3">
          <div className="space-y-2">
            {bound.runtime_variables ? (
              <h3 className="text-xs font-medium text-muted">{t('environment.baseVariables')}</h3>
            ) : null}
            <VariableList variables={bound.variables} sourceLabel={sourceLabel} />
          </div>
          {bound.runtime_variables ? (
            <div className="space-y-2">
              <h3 className="text-xs font-medium text-muted">
                {t('environment.runtimeVariables')}
              </h3>
              <VariableList variables={bound.runtime_variables} sourceLabel={sourceLabel} />
            </div>
          ) : null}
          {(
            [
              ['setup_script', 'environment.installScript'],
              ['start_script', 'environment.startScript'],
            ] as const
          ).map(([key, label]) =>
            bound[key] ? (
              <div key={key}>
                <p className="mb-1 text-xs text-muted">{t(label)}</p>
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-surface-muted p-2 font-mono text-xs">
                  {bound[key]}
                </pre>
              </div>
            ) : null,
          )}
          {bound.binding_id ? (
            <div className="space-y-1">
              <button
                type="button"
                className="ui-button ui-button-secondary"
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    await refreshWorkspaceRuntime(jobId, bound.runtime_revision ?? 0);
                    setReload((v) => v + 1);
                  })
                }
              >
                {t('environment.refreshRuntime')}
              </button>
              <p className="text-xs text-muted">{t('environment.runtimeHint')}</p>
            </div>
          ) : null}
        </div>
      </details>

      <button
        type="button"
        className="ui-button ui-button-secondary"
        disabled={busy}
        onClick={() => void openEditor()}
      >
        {t(editLabel)}
      </button>
      {error || loadError ? (
        <p role="alert" className="text-sm text-status-error">
          {error || loadError}
        </p>
      ) : null}
      {editing ? (
        <EnvironmentDialog
          initial={editing.config}
          globals={editing.globals}
          onSaved={() => setReload((v) => v + 1)}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </section>
  );
}
