'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type {
  AgentConfigApi,
  AgentPluginInputApi,
  CreateAgentJobRequest,
  GoalInputApi,
} from '@/lib/api/agents';
import { isComposerSubmit } from './composerKeys';
import { ConnectSourceControl } from './ConnectSourceControl';
import { NEW_TASK_PROMPT_ID, useFirstRun } from './firstRun';
import { githubAccessUrl, leaveFor } from './githubAccess';
import {
  forgetComposerDraft,
  onGitHubAccessRequest,
  peekComposerDraft,
  rememberReturn,
} from './githubReturn';
import { SOURCE_CONTROL_RECHECK_MS } from './sourceControlRecovery';
import { highestEffort, offeredEfforts } from './effort';
import { useT } from '@/components/providers';
import { useAuth } from '@/components/providers/AuthProvider';
import { useComposerDraft } from './useComposerDraft';
import { Picker } from './Picker';
import { RepositoryPicker } from './RepositoryPicker';
import { CreateRepositoryDialog } from './CreateRepositoryDialog';
import { ComposerPlusMenu } from './ComposerPlusMenu';
import { ComposerCommandMenu } from './ComposerCommandMenu';
import { PromptRewrite } from './PromptRewrite';
import { composerCommandMatches } from './composerCommands';
import { AttachmentStrip } from './attachments/AttachmentStrip';
import { useAttachments } from './attachments/useAttachments';
import { filesFromDataTransfer } from '@/lib/dataTransferFiles';
import { GOAL_COMMAND_PREFIX, useGoalCommand } from './goal/useGoalCommand';
import { useComposerMcp } from './mcp/useComposerMcp';
import { SkillSelectionDialog } from './skills/SkillPicker';
import { useSkillLibrary } from './skills/useSkillLibrary';
import type { SelectedSkillApi } from '@/lib/api/skills';
import { readDraftPlugins, validPluginForRuntime } from '@/lib/pluginConfiguration';
import { PluginSelectionDialog } from './plugins/PluginSelectionDialog';
import { UserAvatar } from './UserAvatar';

// New-task composer (the /agents index state). Runtime × model are first-class
// controls — BYOA × BYOM is the product, not an advanced option.
//
// Everything here is read from the backend rather than written into the markup.
// The previous version showed a repository, a branch, a runtime and a model as
// fixed labels and then submitted different hardcoded values, so the screen
// described a job nobody was running; and it showed those controls whether or
// not any source control was connected, so it looked ready when nothing it
// produced could run.

export function TaskComposer({ initialRepo }: { initialRepo?: string }) {
  const { state } = useAuth();
  const userId = state.user?.id;
  return (
    <TaskComposerContent key={userId ?? 'anonymous'} userId={userId} initialRepo={initialRepo} />
  );
}

interface NewTaskDraft {
  task: string;
  repo: string;
  runtime: string;
  model: string;
  effort: string;
  branch: string;
  plugins: AgentPluginInputApi[];
}

function readNewTaskDraft(value: string): NewTaskDraft | null {
  try {
    const saved: unknown = JSON.parse(value);
    if (!saved || typeof saved !== 'object') return null;
    const record = saved as Record<string, unknown>;
    const fields = ['task', 'repo', 'runtime', 'model', 'effort', 'branch'] as const;
    if (!fields.every((field) => typeof record[field] === 'string')) return null;
    const plugins = readDraftPlugins(record.plugins);
    if (plugins === null) return null;
    return { ...(saved as NewTaskDraft), plugins };
  } catch {
    return null;
  }
}

function TaskComposerContent({ initialRepo, userId }: { initialRepo?: string; userId?: string }) {
  const router = useRouter();
  const t = useT();
  const draftStore = useComposerDraft({ userId, threadId: 'new-task' });
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  // Restore this account's unsent text and settings on any return to New task.
  // The older GitHub receipt remains a scoped fallback for an existing trip.
  const [draft] = useState(() => {
    const saved = readNewTaskDraft(draftStore.value);
    const returning = peekComposerDraft(userId);
    return saved ?? (returning ? { ...returning, effort: '', branch: '', plugins: [] } : null);
  });
  const [task, setTask] = useState(() => draft?.task ?? '');
  const skillLibrary = useSkillLibrary();
  const [chosenSkills, setChosenSkills] = useState<SelectedSkillApi[] | null>(null);
  const selectedSkills =
    chosenSkills ?? skillLibrary.skills.filter((skill) => skill.default_enabled);
  const pendingSkillDefaults =
    chosenSkills === null && (skillLibrary.loading || !!skillLibrary.error);
  // The attach menu's own state. What this message will carry, as opposed to
  // how it runs; an untouched composer sends neither `skill_ids` nor a goal.
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [plugins, setPlugins] = useState<AgentPluginInputApi[]>(() => draft?.plugins ?? []);
  const [pluginsOpen, setPluginsOpen] = useState(false);
  // What to say when `/goal` was typed with nothing after it. Guidance, not a
  // failure: nothing was created and nothing was sent.
  const [goalHint, setGoalHint] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const taskBox = useRef<HTMLTextAreaElement>(null);
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  // Empty until the owner picks one. A task changes a real repository, so
  // where it runs is never a default — see RepositoryPicker.
  const [repo, setRepo] = useState(draft?.repo ?? '');
  const [repoPickerOpen, setRepoPickerOpen] = useState(false);
  // Enter with no repository opens the picker; the choice then hands the caret
  // back to the task, so a second Enter sends it.
  const [repoPickerFromTask, setRepoPickerFromTask] = useState(false);
  // A repository asked for by the link that opened this page, which this
  // account cannot use. Said out loud rather than swapped for another one.
  const [unavailableRepo, setUnavailableRepo] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [createRepositoryOpen, setCreateRepositoryOpen] = useState(false);
  // Which trip to GitHub is starting: connecting, or adding repository access.
  const [leaving, setLeaving] = useState<'connect' | 'manage' | null>(null);
  // The owner tried to run a task written before connecting GitHub. The card
  // above is then the step still missing, so it is pointed at rather than the
  // key ignored — and the text stays, because the trip to GitHub keeps it.
  const [connectNudged, setConnectNudged] = useState(false);
  const headingId = useId();
  const keyboardHintId = useId();
  // Someone with no conversation yet is told, in one line, what a task becomes.
  const firstVisit = useFirstRun().newcomer;
  const [branches, setBranches] = useState<string[]>([]);
  const [branch, setBranch] = useState(draft?.branch ?? '');
  const [branchOptionsRepo, setBranchOptionsRepo] = useState<string | null>(null);
  const [runtime, setRuntime] = useState(draft?.runtime ?? '');
  const [model, setModel] = useState(draft?.model ?? '');
  const [effort, setEffort] = useState(draft?.effort ?? '');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [rewriting, setRewriting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const attachments = useAttachments(t, config?.attachment_limits);
  const mcp = useComposerMcp(t);
  const saveDraft = draftStore.setValue;
  const savedBranch =
    branchOptionsRepo === repo ? branch : draft?.repo === repo ? draft.branch : '';
  useEffect(() => {
    saveDraft(JSON.stringify({ task, repo, runtime, model, effort, branch: savedBranch, plugins }));
  }, [saveDraft, task, repo, runtime, model, effort, savedBranch, plugins]);
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

  const applyConfig = useCallback((cfg: AgentConfigApi) => {
    // The config's model list is the create endpoint's own predicate;
    // /v1/models answers for the browsing user and offered models whose
    // first agent call then 404ed.
    const modelIds = cfg.models ?? [];
    setConfig(cfg);
    setModels(modelIds);
    // A re-read keeps every choice still on offer and drops only the ones that
    // are not. The repository is never filled in: see the effect below.
    setRepo((current) => (current && cfg.repos.includes(current) ? current : ''));
    setRuntime((current) =>
      current && cfg.runtimes.includes(current) ? current : (cfg.runtimes[0] ?? ''),
    );
    const defaultModel = modelIds.includes('deepseek-v4-flash')
      ? 'deepseek-v4-flash'
      : (modelIds[0] ?? '');
    setModel((current) => (current && modelIds.includes(current) ? current : defaultModel));
  }, []);

  // The one place a repository is chosen for the owner: they opened the page
  // from a project's "New task in …", which is them choosing it. Applied when
  // the request changes rather than on every config read, so refreshing the
  // list cannot overwrite a choice made since — and plain "New task" clears
  // whatever the last project link chose. An unavailable request is reported
  // and leaves the choice empty. It used to fall back to the first repository
  // in the list: a task for one repository, silently sent to another.
  //
  // Not while GitHub is disconnected or down: the list is empty then for a
  // reason that has nothing to do with the request, which is kept for when
  // the repositories come back.
  const appliedRequest = useRef<string | null>(null);
  useEffect(() => {
    if (!config?.github_connected || appliedRequest.current === (initialRepo ?? '')) return;
    const first = appliedRequest.current === null;
    appliedRequest.current = initialRepo ?? '';
    // Back from GitHub: the owner's own last choices outrank the link that
    // opened the page, wherever they are still on offer.
    if (first && draft) {
      if (draft.runtime && config.runtimes.includes(draft.runtime)) setRuntime(draft.runtime);
      if (draft.model && (config.models ?? []).includes(draft.model)) setModel(draft.model);
      if (
        (!initialRepo || draft.repo === initialRepo) &&
        draft.repo &&
        config.repos.includes(draft.repo)
      ) {
        setRepo(draft.repo);
        setUnavailableRepo(null);
        return;
      }
    }
    const available = Boolean(initialRepo && config.repos.includes(initialRepo));
    setRepo(available ? initialRepo! : '');
    setUnavailableRepo(initialRepo && !available ? initialRepo : null);
    // Back with a task written and nowhere to run it yet — the usual shape of
    // a first visit, which connects GitHub mid-sentence. Ask where, as Enter
    // would have, so the next key sends it.
    if (first && draft?.task.trim() && !available && config.repos.length > 0) {
      setRepoPickerFromTask(true);
      setRepoPickerOpen(true);
    }
  }, [config, draft, initialRepo]);

  useEffect(() => {
    if (draft) forgetComposerDraft(userId);
  }, [draft, userId]);

  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((cfg) => {
        if (!cancelled) applyConfig(cfg);
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
  }, [applyConfig]);

  // While the deployment's GitHub App is temporarily unavailable nothing can
  // run, and the service is retrying on its own. Look again at its interval so
  // the composer comes back by itself; the pickers are hidden meanwhile, so
  // re-reading the config overwrites no choice the user made.
  const githubUnavailable = config?.github_load_status === 'unavailable';
  useEffect(() => {
    if (!githubUnavailable) return undefined;
    let cancelled = false;
    let pending = false;
    const read = () => {
      if (document.visibilityState === 'hidden' || pending) return;
      pending = true;
      getAgentConfig()
        .then((cfg) => {
          if (!cancelled) applyConfig(cfg);
        })
        // The next tick asks again; the notice already says why nothing runs.
        .catch(() => undefined)
        .finally(() => {
          pending = false;
        });
    };
    const timer = window.setInterval(read, SOURCE_CONTROL_RECHECK_MS);
    document.addEventListener('visibilitychange', read);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', read);
    };
  }, [githubUnavailable, applyConfig]);

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

  // Branches follow the selected repository, so they reload when it changes —
  // and are dropped at once, so the previous repository's branch can never be
  // sent with this one while the new list is on its way.
  useEffect(() => {
    setBranchOptionsRepo(null);
    setBranches([]);
    setBranch('');
    if (!repo || !config?.github_connected) return undefined;
    let cancelled = false;
    listRepoBranches(repo)
      .then((found) => {
        if (cancelled) return;
        setBranches(found.branches);
        setBranchOptionsRepo(repo);
        const restored = draft?.repo === repo ? draft.branch : '';
        setBranch(
          found.branches.includes(restored) ? restored : (found.default ?? found.branches[0] ?? ''),
        );
      })
      .catch(() => {
        // No branch list is a working state: the job then runs against the
        // repository's own default, which is what omitting a ref means.
        if (!cancelled) {
          setBranches([]);
          setBranch('');
          setBranchOptionsRepo(repo);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [config?.github_connected, repo, draft]);

  // `mcp_runtimes` has been reported by /config since MCP landed and read by
  // nothing; an older control plane omits it, and not knowing is not the same
  // as knowing the runtime cannot.
  const mcpRuntimes = config?.mcp_runtimes;
  const mcpBlockedReason =
    runtime && mcpRuntimes && !mcpRuntimes.includes(runtime)
      ? t('attach.mcpUnsupported', { runtime })
      : undefined;
  const pluginsSupported = config?.plugin_runtimes?.includes(runtime) ?? false;
  const incompatiblePlugins = plugins.some((plugin) => !validPluginForRuntime(runtime, plugin));
  const pluginBlockedReason = !pluginsSupported
    ? t('plugins.unsupported', { runtime })
    : incompatiblePlugins
      ? t('plugins.incompatible', { runtime })
      : undefined;
  const pluginsBlocked = plugins.length > 0 && (!pluginsSupported || incompatiblePlugins);

  const disconnected = config?.github_connected === false;
  // Someone who can go and connect may write the task first: the trip keeps
  // it. Where nobody can connect right now — the deployment has no App, or its
  // App is down — there is nothing for a draft to wait for.
  const canConnect = Boolean(config?.github_install_url) && !githubUnavailable;
  const locked = disconnected && !canConnect;
  // Connected, and GitHub has given the App no repository yet: a step still to
  // take, not a connection to make again.
  const noRepositories = config?.github_connected === true && config.repos.length === 0;
  const busy = submitting || goalCommand.busy || rewriting;
  const missingRepository = config?.github_connected === true && !repo;
  const canRun =
    config?.github_connected === true &&
    task.trim().length > 0 &&
    !busy &&
    // A file still in flight has no id yet. Running now would queue a job
    // without the attachment the user can see on screen.
    !attachments.uploading &&
    !pluginsBlocked &&
    Boolean(repo && runtime && model);

  /**
   * Go to GitHub for one step and come back here with the draft intact.
   *
   * Straight to GitHub rather than by way of the Integrations page, which used
   * to be where the trip ended — away from the task the owner was writing.
   */
  async function goToGitHub(kind: 'connect' | 'manage') {
    setLeaving(kind);
    setError(null);
    rememberReturn(
      initialRepo ? `/?repo=${encodeURIComponent(initialRepo)}` : '/',
      {
        task,
        repo,
        runtime,
        model,
      },
      userId,
    );
    try {
      const url = await githubAccessUrl(kind);
      if (url) {
        leaveFor(url);
        return;
      }
      router.push('/integrations');
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('composer.loadConfigFailed'));
    }
    setLeaving(null);
  }

  // A trip asked for elsewhere on the page — the sidebar's getting-started
  // step — leaves from here, so the draft in this box goes with it.
  const goToGitHubRef = useRef(goToGitHub);
  goToGitHubRef.current = goToGitHub;
  useEffect(() => onGitHubAccessRequest((kind) => void goToGitHubRef.current(kind)), []);

  function openRepositoryPicker(fromTask: boolean) {
    setRepoPickerFromTask(fromTask);
    setRepoPickerOpen(true);
  }

  function chooseRepository(next: string) {
    setRepo(next);
    setUnavailableRepo(null);
  }

  /**
   * Read the repository list again, for a repository just given to the App.
   * GitHub is the one that changed, so this is a re-read and nothing more:
   * every choice still on offer survives it.
   */
  async function refreshRepositories() {
    setRefreshing(true);
    try {
      const cfg = await getAgentConfig({ fresh: true });
      applyConfig(cfg);
      setError(null);
      // The repository the link asked for may be the one that just arrived.
      if (unavailableRepo && cfg.repos.includes(unavailableRepo)) {
        setRepo(unavailableRepo);
        setUnavailableRepo(null);
      }
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('composer.loadConfigFailed'));
    } finally {
      setRefreshing(false);
    }
  }

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
      // Switching menus while typing a command completes its partial token.
      if (composerCommandMatches(current).some((entry) => entry.name === 'goal')) {
        return GOAL_COMMAND_PREFIX;
      }
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
      ...(plugins.length ? { plugins } : {}),
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
    if (pluginsBlocked) throw new Error(pluginBlockedReason);
    const snapshot = draftStore.capture();
    setGoalHint(null);
    const job = await createAgentJob(requestBody(objective, { objective }));
    draftStore.acknowledge(snapshot);
    if (mounted.current) router.push(`/${job.id}`);
  }

  async function run() {
    if (!canRun) {
      // Written before connecting GitHub: point at the step that is missing.
      if (disconnected && canConnect && task.trim() && !busy) {
        setConnectNudged(true);
        return;
      }
      // The one thing missing is where to run it: ask, rather than ignore the
      // key. Nothing is sent until a repository has actually been chosen.
      if (missingRepository && task.trim() && !busy) openRepositoryPicker(true);
      return;
    }
    // A command is not a message. It runs the thing it names and, when that
    // succeeds, leaves nothing in the box to send to the model.
    if (await goalCommand.submit()) return;
    setSubmitting(true);
    setError(null);
    try {
      const snapshot = draftStore.capture();
      const job = await createAgentJob(requestBody(task.trim()));
      draftStore.acknowledge(snapshot);
      if (mounted.current) router.push(`/${job.id}`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : t('composer.startFailed'));
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <section
        aria-busy="true"
        className="mx-auto w-full max-w-3xl px-4 pb-10 pt-10 sm:px-8 md:pt-16"
      >
        <div
          aria-hidden="true"
          className="mx-auto h-16 w-16 rounded-2xl bg-surface-hover motion-safe:animate-pulse"
        />
        <p aria-live="polite" className="mt-6 text-center text-sm text-muted">
          {t('common.loading')}
        </p>
        <div
          aria-hidden="true"
          className="mt-8 h-64 rounded-2xl border border-border bg-surface-muted motion-safe:animate-pulse"
        />
      </section>
    );
  }

  return (
    <section className="ui-enter mx-auto w-full max-w-3xl px-4 pb-10 pt-8 sm:px-8 md:pt-14 lg:pt-16">
      <div className="ui-welcome-enter">
        <UserAvatar />
        <h1
          id={headingId}
          className="text-balance text-center text-[28px] font-semibold leading-tight tracking-[-0.035em] text-foreground sm:text-[34px] lg:text-[36px]"
        >
          {t('composer.heading')}
        </h1>
        <p className="mx-auto mt-3 max-w-md text-balance text-center text-sm leading-6 text-muted">
          {firstVisit ? t('composer.subheadingFirstVisit') : t('composer.subheading')}
        </p>
      </div>

      {disconnected ? (
        <div className="mt-8">
          <ConnectSourceControl
            installUrl={config.github_install_url}
            unavailable={githubUnavailable}
            onAction={() => void goToGitHub('connect')}
            pending={leaving === 'connect'}
            highlight={connectNudged}
          />
        </div>
      ) : noRepositories ? (
        <div className="mt-8">
          <ConnectSourceControl
            installUrl={config.github_install_url}
            step="repositories"
            onAction={() => void goToGitHub('manage')}
            pending={leaving === 'manage'}
          />
          <button
            type="button"
            disabled={rewriting || submitting}
            onClick={() => {
              if (!rewriting && !submitting) setCreateRepositoryOpen(true);
            }}
            className="mt-3 rounded-lg px-3 py-2 text-sm font-medium text-crimson-dark hover:bg-crimson/5 disabled:cursor-not-allowed disabled:opacity-50 dark:text-rose-300 dark:hover:bg-white/5"
          >
            {t('repositoryCreate.title')}
          </button>
        </div>
      ) : null}

      <div
        aria-disabled={locked || undefined}
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
        className={`ui-composer ${
          dropping ? 'border-accent/40 ring-4 ring-accent/[0.08]' : ''
        } ${disconnected || noRepositories ? 'mt-4' : 'mt-8'} ${locked ? 'bg-surface-muted' : ''}`}
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
          onPaste={(event) => {
            if (disconnected || busy) return;
            attachments.onPaste(event);
          }}
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
          // No placeholder: the heading above already asks the question, and
          // an example task read as a suggestion to run it.
          id={NEW_TASK_PROMPT_ID}
          aria-labelledby={headingId}
          aria-describedby={keyboardHintId}
          disabled={locked}
          className="block min-h-[150px] w-full resize-y rounded-t-2xl border-0 bg-transparent px-5 pb-4 pt-5 text-base leading-relaxed text-foreground focus:outline-none focus:ring-0 disabled:cursor-not-allowed sm:min-h-[160px] sm:px-6"
        />

        {disconnected ? null : (
          <AttachmentStrip
            entries={attachments.entries}
            onRemove={attachments.remove}
            disabled={submitting}
          />
        )}

        {!disconnected && (
          <PromptRewrite
            value={task}
            model={model}
            disabled={submitting || goalCommand.busy || /^\s*\//.test(task)}
            onActiveChange={setRewriting}
            onApply={(prompt) => {
              setTask(prompt);
              taskBox.current?.focus();
            }}
          />
        )}

        <div className="flex flex-col gap-2 rounded-b-2xl border-t border-border bg-surface-muted/50 p-3 sm:px-4">
          <div className="flex min-w-0 flex-wrap items-center gap-1">
            {disconnected ? (
              <span
                aria-live="polite"
                className={`inline-flex min-w-0 flex-1 items-center gap-2 text-[13px] ${
                  connectNudged
                    ? 'text-crimson-dark dark:text-crimson-light'
                    : 'text-gray-400 dark:text-gray-500'
                }`}
              >
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
                {connectNudged ? t('composer.connectToRunKept') : t('composer.connectToRun')}
              </span>
            ) : (
              <>
                <ComposerPlusMenu
                  disabled={busy}
                  onOpen={goalCommand.dismiss}
                  onFiles={attachments.add}
                  onOpenSkills={() => setSkillsOpen(true)}
                  skillSummary={
                    pendingSkillDefaults
                      ? t('attach.skillsDefaults')
                      : String(selectedSkills.length)
                  }
                  goalSummary={t('commands.goalHint')}
                  onOpenGoal={insertGoalCommand}
                  mcp={mcp}
                  mcpBlockedReason={mcpBlockedReason}
                  onOpenPlugins={
                    pluginsSupported || plugins.length ? () => setPluginsOpen(true) : undefined
                  }
                  pluginSummary={String(plugins.length)}
                />
                <span aria-hidden="true" className="mx-1 h-5 w-px bg-border" />
                <RepositoryPicker
                  repos={config?.repos ?? []}
                  value={repo}
                  onChange={chooseRepository}
                  open={repoPickerOpen}
                  onOpenChange={(next) => {
                    if (next) openRepositoryPicker(false);
                    else setRepoPickerOpen(false);
                  }}
                  onRefresh={() => void refreshRepositories()}
                  refreshing={refreshing}
                  onConfigure={
                    config?.github_install_url ? () => void goToGitHub('manage') : undefined
                  }
                  onCreate={() => {
                    if (!rewriting && !submitting) setCreateRepositoryOpen(true);
                  }}
                  createDisabled={rewriting || submitting}
                  returnFocusTo={repoPickerFromTask ? taskBox : undefined}
                />
                <Picker
                  label={t('composer.branch')}
                  value={branch}
                  options={branches}
                  onChange={setBranch}
                />
              </>
            )}
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-1">
            {!disconnected && (
              <>
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
              title={missingRepository ? t('composer.chooseRepository') : t('composer.runTitle')}
              className="ui-button ui-button-primary ml-auto shrink-0 px-4 text-xs shadow-sm"
            >
              {submitting || goalCommand.busy ? t('composer.starting') : t('composer.run')}
              <svg
                aria-hidden="true"
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
      </div>

      {pluginsBlocked ? (
        <p role="alert" className="mt-3 text-center text-sm text-status-error">
          {pluginBlockedReason}{' '}
          <button type="button" className="underline" onClick={() => setPluginsOpen(true)}>
            {t('plugins.manage')}
          </button>
        </p>
      ) : null}

      <p id={keyboardHintId} className="mt-4 text-center text-xs leading-relaxed text-muted">
        {t('composer.keyboardHint')}
      </p>

      {pluginsOpen ? (
        <PluginSelectionDialog
          runtime={runtime}
          selected={plugins}
          onChange={setPlugins}
          onClose={() => setPluginsOpen(false)}
          blockedReason={pluginBlockedReason}
        />
      ) : null}
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
      <CreateRepositoryDialog
        open={createRepositoryOpen}
        onClose={() => setCreateRepositoryOpen(false)}
        onAvailable={(repo, config) => {
          applyConfig(config);
          chooseRepository(repo);
          taskBox.current?.focus();
        }}
      />

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

      {unavailableRepo && !disconnected ? (
        <p className="mt-3 text-center text-[13px] text-gray-500 dark:text-gray-400" role="status">
          {t('composer.repositoryUnavailable', { repo: unavailableRepo })}
        </p>
      ) : null}

      {error ? (
        <p className="mt-3 text-center text-[13px] text-red-600 dark:text-red-400" role="alert">
          {error}
          {!config && (
            <button
              type="button"
              disabled={refreshing}
              onClick={() => void refreshRepositories()}
              className="ml-2 underline"
            >
              {t('common.retry')}
            </button>
          )}
        </p>
      ) : null}
      {firstVisit && (
        <div className="mt-8 grid gap-5 border-t border-border pt-6 sm:grid-cols-3">
          {(
            [
              ['composer.repository', 'composer.workflowRepository'],
              ['composer.agent', 'composer.workflowAgent'],
              ['composer.workflowReviewTitle', 'composer.workflowReview'],
            ] as const
          ).map(([title, description], index) => (
            <div key={title} className="flex gap-3 px-1">
              <span
                aria-hidden="true"
                className="mt-0.5 text-xs font-medium tabular-nums text-muted"
              >
                0{index + 1}
              </span>
              <div>
                <h2 className="text-[13px] font-medium text-foreground">{t(title)}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">{t(description)}</p>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
