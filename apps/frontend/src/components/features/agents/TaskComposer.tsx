'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';
import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi, CreateAgentJobRequest, GoalInputApi } from '@/lib/api/agents';
import { isComposerSubmit } from './composerKeys';
import { ConnectSourceControl } from './ConnectSourceControl';
import { highestEffort, offeredEfforts } from './effort';
import { useT } from '@/components/providers';
import { Picker } from './Picker';
import { ComposerPlusMenu } from './ComposerPlusMenu';
import { ComposerCommandMenu } from './ComposerCommandMenu';
import { AttachmentStrip } from './attachments/AttachmentStrip';
import { useAttachments } from './attachments/useAttachments';
import { filesFromDataTransfer } from '@/lib/dataTransferFiles';
import { GOAL_COMMAND_PREFIX, useGoalCommand } from './goal/useGoalCommand';
import { useComposerMcp } from './mcp/useComposerMcp';
import { SkillSelectionDialog } from './skills/SkillPicker';
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
  // The attach menu's own state. What this message will carry, as opposed to
  // how it runs; an untouched composer sends neither `skill_ids` nor a goal.
  const [skillsOpen, setSkillsOpen] = useState(false);
  // What to say when `/goal` was typed with nothing after it. Guidance, not a
  // failure: nothing was created and nothing was sent.
  const [goalHint, setGoalHint] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const taskBox = useRef<HTMLTextAreaElement>(null);
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
  const attachments = useAttachments(t, config?.attachment_limits);
  const mcp = useComposerMcp(t);
  const goalCommand = useGoalCommand({
    value: task,
    setValue: setTask,
    focusComposer: () => taskBox.current?.focus(),
    onNeedsObjective: () => setGoalHint(t('goal.needsObjective')),
    onApply: applyGoalObjective,
    onError: setError,
  });

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

  // `mcp_runtimes` has been reported by /config since MCP landed and read by
  // nothing; an older control plane omits it, and not knowing is not the same
  // as knowing the runtime cannot.
  const mcpRuntimes = config?.mcp_runtimes;
  const mcpBlockedReason =
    runtime && mcpRuntimes && !mcpRuntimes.includes(runtime)
      ? t('attach.mcpUnsupported', { runtime })
      : undefined;

  const disconnected = config?.github_connected === false;
  const busy = submitting || goalCommand.busy;
  const canRun =
    config?.github_connected === true &&
    task.trim().length > 0 &&
    !busy &&
    // A file still in flight has no id yet. Running now would queue a job
    // without the attachment the user can see on screen.
    !attachments.uploading &&
    Boolean(repo && runtime && model);

  /**
   * The plus menu's Goal row: put `/goal ` in the box and leave the caret after
   * it, so the interaction is typing where the owner was already typing.
   *
   * There is nothing else for the row to open. A goal is an objective and the
   * band above the messages manages it afterwards, so a dialog here would be a
   * second place to say the same one thing — and the one the owner has to
   * dismiss before they can type the sentence they came to type.
   */
  function insertGoalCommand() {
    setGoalHint(null);
    setTask((current) => {
      // Already the command: nothing to add, and prefixing again would make
      // `/goal /goal …` an objective nobody wrote.
      if (/^\s*\/goal(\s|$)/i.test(current)) return current;
      // Anything already typed *is* the objective — the row means "make this
      // message a goal", and an objective is a whole sentence.
      return current.trim() ? `${GOAL_COMMAND_PREFIX}${current}` : GOAL_COMMAND_PREFIX;
    });
    taskBox.current?.focus();
  }

  /**
   * Exactly what the controls show. That this needs saying is the bug it
   * replaced: the old composer displayed one thing and queued another.
   *
   * The prompt and the goal are the only things a command changes; everything
   * else — the repository, the branch, the harness, the model, the skills, the
   * uploads — is what the owner chose on this screen, and rides both paths.
   */
  function requestBody(prompt: string, goal?: GoalInputApi): CreateAgentJobRequest {
    return {
      repo,
      task_prompt: prompt,
      // Defaults are resolved by the server. Only an explicit picker change
      // belongs in the request, so retries are independent of library edits.
      ...(chosenSkills !== null ? { skill_ids: chosenSkills.map((skill) => skill.id) } : {}),
      // Staged uploads, named by id. Omitted when nothing is attached so the
      // request keeps the shape it had before this feature existed.
      ...(attachments.ids.length ? { attachment_ids: attachments.ids } : {}),
      // Omitted gives the task no servers at all, which is the right default:
      // connecting a server and letting a task call it are separate choices.
      ...(mcp.bindings.length ? { mcp_bindings: mcp.bindings } : {}),
      // The completion condition rides the first message, so the first turn
      // is already working toward it rather than learning about it later.
      ...(goal ? { goal } : {}),
      runtime,
      model,
      // Sent as a ref; the platform pins it to a commit at creation, because
      // a branch moves and the publisher applies onto a fixed one.
      ...(branch ? { base_ref: branch } : {}),
      // Whatever the control shows is what is sent. Empty only when the
      // pair offers no control at all, and then there is nothing to say.
      ...(effort ? { reasoning_effort: effort } : {}),
    };
  }

  /**
   * `/goal <objective>` on a task that does not exist yet.
   *
   * **One request, one task.** The objective is the first turn's content *and*
   * the condition that continues it, so the run starts working instead of
   * waiting for a second message saying "begin"; and because it is one call,
   * the command cannot leave a task behind if it fails. A refused creation is
   * thrown rather than drawn here — the command reports it and keeps the text,
   * because the retry is the same command.
   *
   * No `max_rounds`: the goal the owner asked for has no round limit. The field
   * is absent from the request rather than filled with a number, because
   * "unlimited" is the absence of a bound and a large number is not.
   */
  async function applyGoalObjective(objective: string): Promise<void> {
    setGoalHint(null);
    const job = await createAgentJob(requestBody(objective, { objective }));
    router.push(`/${job.id}`);
  }

  async function run() {
    if (!canRun) return;
    // A command is not a message. It runs the thing it names and, when that
    // succeeds, leaves nothing in the box to send to the model.
    if (await goalCommand.submit()) return;
    setSubmitting(true);
    setError(null);
    try {
      const job = await createAgentJob(requestBody(task.trim()));
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
        // Dropping onto the composer is the other way in, and the only one that
        // takes files and folders in a single gesture — a file input can offer
        // one or the other, never both.
        onDragOver={(event) => {
          if (disconnected || !event.dataTransfer.types.includes('Files')) return;
          event.preventDefault();
          setDropping(true);
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
          setDropping(false);
        }}
        onDrop={(event) => {
          if (disconnected) return;
          event.preventDefault();
          setDropping(false);
          void filesFromDataTransfer(event.dataTransfer).then((files) => {
            if (files.length) attachments.add(files);
          });
        }}
        className={`relative rounded-xl border shadow-[0_14px_40px_-28px_rgba(17,24,39,0.5)] transition ${
          dropping
            ? 'border-crimson/40 ring-4 ring-crimson/[0.08]'
            : 'border-gray-200/90 dark:border-white/10'
        } ${
          disconnected
            ? 'mt-4 bg-gray-50 dark:bg-gray-950'
            : 'mt-8 bg-white dark:bg-gray-900 focus-within:border-crimson/30 focus-within:ring-4 focus-within:ring-crimson/[0.05]'
        }`}
      >
        <ComposerCommandMenu
          id={goalCommand.menuId}
          entries={goalCommand.entries}
          activeIndex={goalCommand.activeIndex}
          onHighlight={goalCommand.onHighlight}
          onChoose={goalCommand.onChoose}
        />
        <textarea
          ref={taskBox}
          rows={4}
          value={task}
          onChange={(event) => {
            setTask(event.target.value);
            if (goalHint) setGoalHint(null);
          }}
          onKeyDown={(event) => {
            // The menu owns the keys while it is showing: Enter confirms the
            // highlighted command instead of sending half of one to the model.
            if (goalCommand.onKeyDown(event)) return;
            if (!isComposerSubmit(event)) return;
            event.preventDefault();
            void run();
          }}
          // A command is being chosen, so the list is the box's own popup and
          // the highlighted row is named to it — the caret never leaves here.
          // `aria-expanded` is deliberately not used: the textbox role does not
          // support it, and `aria-controls` pointing at a list that is only in
          // the DOM while it is open says the same thing without lying.
          aria-autocomplete="list"
          aria-controls={goalCommand.open ? goalCommand.menuId : undefined}
          aria-activedescendant={goalCommand.activeId}
          disabled={disconnected}
          placeholder={
            disconnected ? t('composer.placeholderDisconnected') : t('composer.placeholder')
          }
          className="w-full resize-none rounded-t-xl border-0 bg-transparent px-5 pb-2 pt-4 text-[15px] leading-relaxed text-gray-900 dark:text-gray-50 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:ring-0 disabled:cursor-not-allowed"
        />

        {disconnected ? null : (
          <AttachmentStrip
            entries={attachments.entries}
            onRemove={attachments.remove}
            disabled={submitting}
          />
        )}

        <div className="relative flex flex-wrap items-center gap-2 border-t border-gray-100/90 dark:border-white/5 px-3.5 py-2.5">
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
              <ComposerPlusMenu
                disabled={busy}
                onFiles={attachments.add}
                onOpenSkills={() => setSkillsOpen(true)}
                skillSummary={
                  pendingSkillDefaults ? t('attach.skillsDefaults') : String(selectedSkills.length)
                }
                goalSummary={t('attach.goalNone')}
                onOpenGoal={insertGoalCommand}
                mcp={mcp}
                mcpBlockedReason={mcpBlockedReason}
              />
              <span className="h-4 w-px bg-gray-200 dark:bg-white/10" />
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
            {busy ? t('composer.starting') : t('composer.run')}
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

      {skillsOpen ? (
        <SkillSelectionDialog
          limits={config?.skill_limits}
          selected={selectedSkills}
          onClose={() => setSkillsOpen(false)}
          onApply={(skills) => {
            // Only a real change is recorded. Leaving the selection alone keeps
            // `chosenSkills` null, which is what makes the request omit
            // `skill_ids` and let the server resolve account defaults.
            const originalIds = new Set(selectedSkills.map((skill) => skill.id));
            if (
              skills.length !== originalIds.size ||
              skills.some((skill) => !originalIds.has(skill.id))
            ) {
              setChosenSkills(skills);
            }
            setSkillsOpen(false);
          }}
        />
      ) : null}

      {skillLibrary.error ? (
        <div className="mt-3 text-center text-sm text-red-600 dark:text-red-400" role="alert">
          {skillLibrary.error} You can still start a task with your account defaults.{' '}
          <button type="button" onClick={() => void skillLibrary.reload()} className="underline">
            Retry loading skills
          </button>
        </div>
      ) : null}

      {/* `/goal` on its own is not an objective. It is consumed rather than
          sent to the model, and this is where the owner is told what is
          missing — beside the box the token is still sitting in. */}
      {goalHint ? (
        <p className="mt-3 text-center text-[13px] text-gray-500 dark:text-gray-400" role="status">
          {goalHint}
        </p>
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
