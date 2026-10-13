'use client';

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  getEnvironmentActionRun,
  getWorkspaceEnvironment,
  refreshWorkspaceRuntime,
  skipWorkspaceStart,
  retryWorkspaceEnvironment,
  startEnvironmentAction,
  stopEnvironmentAction,
} from '@/lib/api/environments';
import type { ActionRun, EnvironmentBinding } from '@/lib/api/environments';
import { EnvironmentManager } from './EnvironmentManager';
import { EnvironmentSteps } from './EnvironmentEditor';

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
  const [bound, setBound] = useState<EnvironmentBinding | null>(null);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [runs, setRuns] = useState<Record<string, ActionRun>>({});
  const [busy, setBusy] = useState(false);
  const pendingKeys = useRef<Record<string, string>>({});
  const [reload, setReload] = useState(0);
  const [skipArmed, setSkipArmed] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void getWorkspaceEnvironment(jobId)
        .then((value) => {
          if (active) setBound(value);
        })
        .catch((e) => {
          if (active) setError(e instanceof Error ? e.message : String(e));
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
    const current = Object.entries(runs).filter(([, run]) =>
      ['starting', 'running', 'paused', 'suspended'].includes(run.state),
    );
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
  return (
    <section className="space-y-4 p-4" aria-label={t('environment.current')}>
      <h2 className="text-sm font-semibold">{t('environment.current')}</h2>
      {bound && (
        <>
          <p className="text-xs text-muted">
            {bound.environment_name ?? t('environment.platformBase')} ·{' '}
            {t('environment.boundVersion')} v{bound.revision ?? 0} ·{' '}
            {t('environment.globalVariables')} v{bound.global_revision ?? 0}
          </p>
          {bound.has_new_version && (
            <p className="rounded border border-border p-2 text-xs">
              {t('environment.newVersionAvailable')}{' '}
              <a href={`/?repo=${encodeURIComponent(repo)}`} className="text-accent underline">
                {t('environment.newWorkspace')}
              </a>
            </p>
          )}
          {bound.binding_id && (
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
          )}
          <p className="text-xs text-muted">
            {t('environment.runtimeHint')} · v{bound.runtime_revision ?? 0}
          </p>
          {bound.runtime_variables && (
            <h3 className="text-xs font-medium">{t('environment.baseVariables')}</h3>
          )}
          <ul className="space-y-1 text-xs">
            {bound.variables.map((v) => (
              <li key={v.name}>
                <code>{v.name}</code> · {v.secret ? t('environment.secretSet') : v.value} ·{' '}
                {v.phase}
              </li>
            ))}
          </ul>
          {bound.runtime_variables && (
            <h3 className="text-xs font-medium">{t('environment.runtimeVariables')}</h3>
          )}
          {bound.runtime_variables && (
            <ul className="space-y-1 text-xs" aria-label={t('environment.refreshRuntime')}>
              {bound.runtime_variables.map((v) => (
                <li key={v.name}>
                  <code>{v.name}</code> · {v.secret ? t('environment.secretSet') : v.value} ·
                  runtime
                </li>
              ))}
            </ul>
          )}
          <details className="text-sm">
            <summary>{t('environment.installScript')}</summary>
            <pre className="overflow-auto whitespace-pre-wrap text-xs">{bound.setup_script}</pre>
          </details>
          <details className="text-sm">
            <summary>{t('environment.startScript')}</summary>
            <pre className="overflow-auto whitespace-pre-wrap text-xs">{bound.start_script}</pre>
          </details>
          <EnvironmentSteps run={{ steps: bound.steps }} />
          {bound.steps.some((s) => s.state === 'failed') && (
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
          )}
          {bound.steps.some((s) => s.phase === 'start' && s.state === 'failed') && (
            <div>
              <button
                type="button"
                className="ui-button ui-button-secondary"
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
              <p className="text-xs text-muted">
                {t(skipArmed ? 'environment.skipArmed' : 'environment.skipHint')}
              </p>
            </div>
          )}
          <div className="space-y-2">
            <h3 className="text-sm font-semibold">{t('environment.actions')}</h3>
            {bound.actions.map((action) => {
              const run = runs[action.id];
              const running =
                run && ['starting', 'running', 'paused', 'suspended'].includes(run.state);
              return (
                <div key={action.id} className="space-y-2 rounded-lg border border-border p-3">
                  <div className="flex flex-wrap justify-between gap-2">
                    <span className="text-sm">
                      {action.name}
                      {run ? ` · ${run.state}` : ''}
                    </span>
                    <div className="flex gap-1">
                      <button
                        type="button"
                        className="ui-button ui-button-secondary"
                        disabled={busy || !!running}
                        onClick={() =>
                          void perform(async () => {
                            const key = pendingKeys.current[action.id] ?? crypto.randomUUID();
                            pendingKeys.current[action.id] = key;
                            const result = await startEnvironmentAction(jobId, action.id, key);
                            delete pendingKeys.current[action.id];
                            setRuns((v) => ({ ...v, [action.id]: result }));
                          })
                        }
                      >
                        {t('environment.run')}
                      </button>
                      {running && (
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
                      )}
                      <button
                        type="button"
                        className="ui-button ui-button-ghost"
                        onClick={onTerminal}
                      >
                        {t('environment.openOutput')}
                      </button>
                    </div>
                  </div>
                  {run?.output && (
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs">
                      {run.output}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
      <button
        type="button"
        className="ui-button ui-button-secondary"
        onClick={() => setEditing(!editing)}
      >
        {t('environment.editSaved')}
      </button>
      {editing && <EnvironmentManager repo={repo} onChanged={() => setReload((v) => v + 1)} />}
      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
