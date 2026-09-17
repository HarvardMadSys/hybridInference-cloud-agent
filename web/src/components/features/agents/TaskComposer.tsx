'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';
import { isComposerSubmit } from './composerKeys';
import { ConnectSourceControl } from './ConnectSourceControl';
import { highestEffort, offeredEfforts } from './effort';
import { useT } from '@/components/providers';
import { Picker } from './Picker';
import { SkillPicker } from './skills/SkillPicker';
import { useSkillLibrary } from './skills/useSkillLibrary';
import type { SelectedSkillApi } from '@/lib/api/skills';

// New-task composer (the /agents index state). Runtime × model are first-class
// controls — BYOA × BYOM is the product, not an advanced option.
//
// Everything here is read from the backend rather than written into the markup.
// The previous version showed a repository, a branch, a runtime and a model as
// fixed labels and then submitted different hardcoded values, so the screen
// described a job nobody was running; and it showed those controls whether or
// not any source control was connected, so it looked ready when nothing it
// produced could run.
const TIER_LABELS: Record<string, string> = {
  platform_only: 'PlatformOnly',
  trusted: 'Trusted',
  custom: 'Custom',
  full: 'Open',
};

export function TaskComposer({ initialRepo }: { initialRepo?: string }) {
  const router = useRouter();
  const t = useT();
  const [task, setTask] = useState('');
  const skillLibrary = useSkillLibrary();
  const [chosenSkills, setChosenSkills] = useState<SelectedSkillApi[] | null>(null);
  const selectedSkills =
    chosenSkills ?? skillLibrary.skills.filter((skill) => skill.default_enabled);
  const pendingSkillDefaults =
    chosenSkills === null && (skillLibrary.loading || !!skillLibrary.error);
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [repo, setRepo] = useState('');
  const [branches, setBranches] = useState<string[]>([]);
  const [branch, setBranch] = useState('');
  const [runtime, setRuntime] = useState('');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The config fetch runs once on mount, but its fallback message has to be
  // translated. Depending on `t` would refetch the config every time the
  // language changed; a ref keeps the effect's dependency list honest.
  const tRef = useRef(t);
  tRef.current = t;

  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((cfg) => {
        if (cancelled) return;
        // The config's model list is the create endpoint's own predicate;
        // /v1/models answers for the browsing user and offered models whose
        // first agent call then 404ed.
        const modelIds = cfg.models ?? [];
        setConfig(cfg);
        setModels(modelIds);
        setRepo(
          initialRepo && cfg.repos.includes(initialRepo) ? initialRepo : (cfg.repos[0] ?? ''),
        );
        setRuntime(cfg.runtimes[0] ?? '');
        setModel(modelIds[0] ?? '');
      })
      .catch((cause: unknown) => {
        if (!cancelled)
          setError(
            cause instanceof Error ? cause.message : tRef.current('composer.loadConfigFailed'),
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [initialRepo]);

  // The offered efforts follow the (runtime, model) pair, so switching either
  // can empty the set or drop the current choice out of it.
  const efforts = useMemo(() => offeredEfforts(config, runtime, model), [config, runtime, model]);
  useEffect(() => {
    // Only once the config has arrived: before that the offered set is empty
    // because nothing is *known*, not because nothing is offered.
    if (!config) return;
    // Deepest by default, and reset to it whenever the pair changes under a
    // selection it no longer offers. Falling back rather than submitting a
    // level the new pair does not have: the backend would refuse it, and the
    // picker would have shown a value it could not deliver.
    if (!efforts.includes(effort)) setEffort(highestEffort(efforts));
  }, [config, efforts, effort]);

  // Branches follow the selected repository, so they reload when it changes.
  useEffect(() => {
    if (!repo || !config?.github_connected) return undefined;
    let cancelled = false;
    listRepoBranches(repo)
      .then((found) => {
        if (cancelled) return;
        setBranches(found.branches);
        setBranch(found.default ?? found.branches[0] ?? '');
      })
      .catch(() => {
        // No branch list is a working state: the job then runs against the
        // repository's own default, which is what omitting a ref means.
        if (!cancelled) {
          setBranches([]);
          setBranch('');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [config?.github_connected, repo]);

  const disconnected = config?.github_connected === false;
  const canRun =
    config?.github_connected === true &&
    task.trim().length > 0 &&
    !submitting &&
    Boolean(repo && runtime && model);

  async function run() {
    if (!canRun) return;
    setSubmitting(true);
    setError(null);
    try {
      // Exactly what the controls show. That this needs saying is the bug it
      // replaced: the old composer displayed one thing and queued another.
      const job = await createAgentJob({
        repo,
        task_prompt: task.trim(),
        // Defaults are resolved by the server. Only an explicit picker change
        // belongs in the request, so retries are independent of library edits.
        ...(chosenSkills !== null ? { skill_ids: chosenSkills.map((skill) => skill.id) } : {}),
        runtime,
        model,
        // Sent as a ref; the platform pins it to a commit at creation, because
        // a branch moves and the publisher applies onto a fixed one.
        ...(branch ? { base_ref: branch } : {}),
        // Whatever the control shows is what is sent. Empty only when the
        // pair offers no control at all, and then there is nothing to say.
        ...(effort ? { reasoning_effort: effort } : {}),
      });
      router.push(`/${job.id}`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('composer.startFailed'));
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <section className="mx-auto w-full max-w-3xl px-6 pt-28">
        <p className="text-center text-sm text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </p>
      </section>
    );
  }

  return (
    <section className="mx-auto w-full max-w-3xl px-6 pb-20 pt-28">
      <h1 className="text-center text-[28px] font-semibold tracking-[-0.025em] text-gray-950 dark:text-gray-50">
        {t('composer.heading')}
      </h1>
      <p className="mt-2 text-center text-sm leading-relaxed text-gray-500 dark:text-gray-400">
        {t('composer.subheading')}
      </p>

      {disconnected ? (
        <div className="mt-8">
          <ConnectSourceControl installUrl={config.github_install_url} />
        </div>
      ) : null}

      <div
        aria-disabled={disconnected || undefined}
        className={`rounded-xl border border-gray-200/90 dark:border-white/10 shadow-[0_14px_40px_-28px_rgba(17,24,39,0.5)] transition ${
          disconnected
            ? 'mt-4 bg-gray-50 dark:bg-gray-950'
            : 'mt-8 bg-white dark:bg-gray-900 focus-within:border-crimson/30 focus-within:ring-4 focus-within:ring-crimson/[0.05]'
        }`}
      >
        <textarea
          rows={4}
          value={task}
          onChange={(event) => setTask(event.target.value)}
          onKeyDown={(event) => {
            if (!isComposerSubmit(event)) return;
            event.preventDefault();
            void run();
          }}
          disabled={disconnected}
          placeholder={
            disconnected ? t('composer.placeholderDisconnected') : t('composer.placeholder')
          }
          className="w-full resize-none rounded-t-xl border-0 bg-transparent px-5 pb-2 pt-4 text-[15px] leading-relaxed text-gray-900 dark:text-gray-50 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:ring-0 disabled:cursor-not-allowed"
        />

        <div className="flex flex-wrap items-center gap-2 border-t border-gray-100/90 dark:border-white/5 px-3.5 py-2.5">
          {disconnected ? (
            <span className="inline-flex min-w-0 flex-1 items-center gap-2 text-[13px] text-gray-400 dark:text-gray-500">
              <svg
                className="h-4 w-4 shrink-0"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M8 10V7a4 4 0 0 1 8 0v3m-9 0h10a1 1 0 0 1 1 1v8H6v-8a1 1 0 0 1 1-1Z"
                />
              </svg>
              {t('composer.connectToRun')}
            </span>
          ) : (
            <>
              <Picker
                label={t('composer.repository')}
                value={repo}
                options={config?.repos ?? []}
                onChange={setRepo}
              />
              <Picker
                label={t('composer.branch')}
                value={branch}
                options={branches}
                onChange={setBranch}
              />
              <span className="h-4 w-px bg-gray-200 dark:bg-white/10" />
              <Picker
                label={t('composer.agent')}
                value={runtime}
                options={config?.runtimes ?? []}
                onChange={setRuntime}
              />
              <Picker
                label={t('composer.model')}
                value={model}
                options={models}
                onChange={setModel}
              />
              <Picker
                label={t('composer.reasoning')}
                value={effort}
                options={efforts}
                onChange={setEffort}
              />
              {pendingSkillDefaults ? (
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  Skills · account defaults
                </span>
              ) : (
                <SkillPicker
                  limits={config?.skill_limits}
                  selected={selectedSkills}
                  onChange={setChosenSkills}
                  disabled={submitting}
                />
              )}
            </>
          )}

          <button
            type="button"
            disabled={!canRun}
            onClick={() => void run()}
            title={t('composer.runTitle')}
            className={`ml-auto inline-flex items-center gap-1.5 rounded-lg bg-crimson px-3.5 py-1.5 text-[13px] font-medium text-white shadow-sm transition-colors ${
              canRun ? 'hover:bg-crimson-dark' : 'cursor-not-allowed opacity-60'
            }`}
          >
            {submitting ? t('composer.starting') : t('composer.run')}
            <svg
              className="h-3.5 w-3.5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14m0 0-6-6m6 6-6 6" />
            </svg>
          </button>
        </div>
      </div>

      {skillLibrary.error ? (
        <div className="mt-3 text-center text-sm text-red-600 dark:text-red-400" role="alert">
          {skillLibrary.error} You can still start a task with your account defaults.{' '}
          <button type="button" onClick={() => void skillLibrary.reload()} className="underline">
            Retry loading skills
          </button>
        </div>
      ) : null}

      {error ? (
        <p className="mt-3 text-center text-[13px] text-red-600 dark:text-red-400" role="alert">
          {error}
        </p>
      ) : null}

      {/* Only what this deployment is actually running. Budget was shown here as
          a fixed "$2.00" the composer never sent — it is a backend cap, not a
          choice made on this screen, so it belongs on the job instead. */}
      {config?.github_connected && config.agent_egress_tier ? (
        <p className="mt-3 text-center text-[12px] text-gray-500 dark:text-gray-400">
          {t('composer.network', {
            setup: TIER_LABELS[config.setup_egress_tier ?? ''] ?? config.setup_egress_tier ?? '',
            agent: TIER_LABELS[config.agent_egress_tier] ?? config.agent_egress_tier,
          })}
        </p>
      ) : null}
    </section>
  );
}
