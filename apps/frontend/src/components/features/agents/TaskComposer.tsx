'use client';

import { useRouter } from 'next/navigation';
import { lazy, Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
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
import { DeferredCreateRepositoryDialog } from './DeferredCreateRepositoryDialog';
import { ComposerDialogLoading } from './ComposerDialogLoading';
import { ComposerPlusMenu } from './ComposerPlusMenu';
import { ComposerCommandMenu } from './ComposerCommandMenu';
import { PromptRewrite } from './PromptRewrite';
import { composerCommandMatches } from './composerCommands';
import { AttachmentStrip } from './attachments/AttachmentStrip';
import { useAttachments } from './attachments/useAttachments';
import { filesFromDataTransfer } from '@/lib/dataTransferFiles';
import { GOAL_COMMAND_PREFIX, useGoalCommand } from './goal/useGoalCommand';
import { useComposerMcp } from './mcp/useComposerMcp';
import { useSkillLibrary } from './skills/useSkillLibrary';
import type { SelectedSkillApi } from '@/lib/api/skills';
import { readDraftPlugins, validPluginForRuntime } from '@/lib/pluginConfiguration';
import { EnvironmentPicker, type EnvironmentSelection } from './environment/EnvironmentPicker';
import { UserAvatar } from './UserAvatar';
import { UserHomeInsights } from './UserHomeInsights';
import { useUserHomeProfile } from './useUserHomeProfile';
import { TEMPORARY_WORKSPACE } from '@/lib/temporaryWorkspace';
import {
  TemporaryWorkspaceConfirm,
  useTemporaryWorkspaceAcknowledgement,
} from './TemporaryWorkspace';

const SkillSelectionDialog = lazy(() =>
  import('./skills/SkillPicker').then((module) => ({ default: module.SkillSelectionDialog })),
);
const PluginSelectionDialog = lazy(() =>
  import('./plugins/PluginSelectionDialog').then((module) => ({
    default: module.PluginSelectionDialog,
  })),
);

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

/**
 * The repositories a task may be sent to now. Choosing one needs a connected
 * GitHub, allowlisted repositories included — `/config` lists those either
 * way — so until then the temporary workspace is the only place to run.
 */
function selectableRepos(config: AgentConfigApi | null): string[] {
  return config?.github_connected ? config.repos : [];
}

function TaskComposerContent({ initialRepo, userId }: { initialRepo?: string; userId?: string }) {
  const router = useRouter();
  const t = useT();
  const userProfile = useUserHomeProfile(userId);
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
  // Drops whose folders are still being walked; their files are not attached yet.
  const [readingDrops, setReadingDrops] = useState(0);
  const taskBox = useRef<HTMLTextAreaElement>(null);
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  // Empty until the owner picks one. A task changes a real repository, so
  // where it runs is never a default — see RepositoryPicker. The one exception
  // is the temporary workspace (`TEMPORARY_WORKSPACE`), which changes no
  // repository at all: with no repository to choose from it is chosen.
  const [repo, setRepo] = useState(draft?.repo ?? '');
  const temporary = repo === TEMPORARY_WORKSPACE;
  const acknowledgement = useTemporaryWorkspaceAcknowledgement(userId);
  // The first temporary workspace's first message waits here for its owner to
  // say they know what one keeps.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [environmentStatus, setEnvironmentStatus] = useState<'loading' | 'ready' | 'error'>(
    'loading',
  );
  const [environmentSelection, setEnvironmentSelection] = useState<EnvironmentSelection | null>(
    null,
  );
  const [environmentOpen, setEnvironmentOpen] = useState(false);
  const [environmentSummary, setEnvironmentSummary] = useState('');
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
    // are not. A repository is never filled in: see the effect below. With no
    // repository on offer — GitHub not connected, down, or giving the App none
    // — the temporary workspace is the only place a task can run, and it
    // changes no real repository, so it is chosen.
    const repos = selectableRepos(cfg);
    setRepo((current) =>
      current === TEMPORARY_WORKSPACE || (current && repos.includes(current))
        ? current
        : repos.length === 0
          ? TEMPORARY_WORKSPACE
          : '',
    );
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
    // "New task" in the temporary workspaces' folder asks for one, which needs
    // no connection and is always on offer.
    if (initialRepo === TEMPORARY_WORKSPACE) {
      if (!config || appliedRequest.current === initialRepo) return;
      appliedRequest.current = initialRepo;
      setRepo(TEMPORARY_WORKSPACE);
      setUnavailableRepo(null);
      return;
    }
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
        (draft.repo === TEMPORARY_WORKSPACE || config.repos.includes(draft.repo))
      ) {
        setRepo(draft.repo);
        setUnavailableRepo(null);
        return;
      }
    }
    const available = Boolean(initialRepo && config.repos.includes(initialRepo));
    setRepo(available ? initialRepo! : config.repos.length === 0 ? TEMPORARY_WORKSPACE : '');
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
    // A temporary workspace has no branches: it starts from one empty commit.
    if (!repo || temporary || !config?.github_connected) return undefined;
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
  }, [config?.github_connected, repo, temporary, draft]);

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
  // Whether the owner can go and connect GitHub right now. Nobody needs to:
  // without a repository a task runs in a temporary workspace.
  const canConnect = Boolean(config?.github_install_url) && !githubUnavailable;
  // A configured App that failed to load stops every new task, a temporary
  // workspace's too: the create endpoint answers 503 until it is back. The
  // composer looks again by itself (above), so there is nothing to do but wait.
  const locked = githubUnavailable;
  // Connected, and GitHub has given the App no repository yet: a step still to
  // take, not a connection to make again.
  const noRepositories = config?.github_connected === true && config.repos.length === 0;
  const busy =
    submitting || goalCommand.busy || rewriting || Boolean(repo && environmentStatus === 'loading');
  const missingRepository = config?.github_connected === true && !repo;
  const canRun =
    !locked &&
    (temporary || (config?.github_connected === true && Boolean(repo))) &&
    task.trim().length > 0 &&
    !busy &&
    !(repo && !environmentSelection && environmentStatus !== 'error') &&
    // A file still in flight has no id yet. Running now would queue a job
    // without the attachment the user can see on screen.
    !attachments.uploading &&
    // Nor while a dropped folder is still being read, before its files exist.
    readingDrops === 0 &&
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
    if (temporary) {
      // Either trip is for a repository: come back to the picker, not to the
      // temporary workspace the owner is leaving to get one.
      saveDraft(
        JSON.stringify({ task, repo: '', runtime, model, effort, branch: savedBranch, plugins }),
      );
    }
    rememberReturn(
      // Not back to the temporary workspaces' folder: its link asks for a
      // temporary workspace again, ahead of the draft that asks where to run.
      initialRepo && initialRepo !== TEMPORARY_WORKSPACE
        ? `/?repo=${encodeURIComponent(initialRepo)}`
        : '/',
      {
        task,
        repo: temporary ? '' : repo,
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
      router.push('/settings/integrations');
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
      // Null starts a temporary workspace; the server pins its empty base.
      repo: temporary ? null : repo,
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
      ...(environmentSelection ?? {}),
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

  async function run(acknowledged = acknowledgement.acknowledged) {
    if (!canRun) {
      // The one thing missing is where to run it: ask, rather than ignore the
      // key. Nothing is sent until a repository has actually been chosen.
      if (missingRepository && task.trim() && !busy) openRepositoryPicker(true);
      return;
    }
    // The first temporary workspace waits for its owner to say they know its
    // files are kept nowhere else. The draft stays in the box meanwhile.
    if (temporary && !acknowledged) {
      setConfirmOpen(true);
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
        className="mx-auto w-full max-w-3xl px-4 pb-8 pt-8 sm:px-6 md:pt-10"
      >
        <div
          aria-hidden="true"
          className="mx-auto h-[152px] w-[152px] rounded-[32px] bg-surface-hover motion-safe:animate-pulse"
        />
        <p aria-live="polite" className="mt-4 text-center text-sm text-muted">
          {t('common.loading')}
        </p>
        <div
          aria-hidden="true"
          className="mt-5 h-40 rounded-xl border border-border bg-surface-muted motion-safe:animate-pulse"
        />
      </section>
    );
  }

  return (
    <section className="ui-enter mx-auto w-full max-w-3xl px-4 pb-8 pt-6 sm:px-6 md:pt-10">
      <div className="ui-welcome-enter">
        <UserAvatar view={userProfile.view} />
        <h1
          id={headingId}
          className="text-balance text-center text-xl font-semibold leading-tight tracking-tight text-foreground sm:text-2xl"
        >
          {t('composer.heading')}
        </h1>
        <p className="mx-auto mt-1.5 max-w-md text-balance text-center text-[13px] leading-5 text-muted">
          {temporary && firstVisit
            ? t('composer.subheadingTemporary')
            : firstVisit
              ? t('composer.subheadingFirstVisit')
              : t('composer.subheading')}
        </p>
      </div>

      {locked ? (
        <div className="mt-5">
          <ConnectSourceControl unavailable />
        </div>
      ) : noRepositories ? (
        <div className="mt-5">
          <ConnectSourceControl
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
          if (locked || !event.dataTransfer.types.includes('Files')) return;
          event.preventDefault();
          setDropping(true);
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
          setDropping(false);
        }}
        onDrop={(event) => {
          if (locked) return;
          event.preventDefault();
          setDropping(false);
          setReadingDrops((count) => count + 1);
          void filesFromDataTransfer(event.dataTransfer)
            .then((files) => {
              if (files.length) attachments.add(files);
            })
            .finally(() => setReadingDrops((count) => count - 1));
        }}
        className={`ui-composer ${
          dropping ? 'border-accent/40 ring-4 ring-accent/[0.08]' : ''
        } ${locked || noRepositories ? 'mt-3' : 'mt-5'} ${locked ? 'bg-surface-muted' : ''}`}
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
            if (locked || busy) return;
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
          disabled={locked}
          aria-labelledby={headingId}
          aria-describedby={keyboardHintId}
          className="block min-h-[88px] w-full resize-y rounded-t-xl border-0 bg-transparent px-4 pb-2 pt-3 text-sm leading-6 text-foreground focus:outline-none focus:ring-0 disabled:cursor-not-allowed sm:min-h-[104px]"
        />

        <AttachmentStrip
          entries={attachments.entries}
          onRemove={attachments.remove}
          disabled={submitting}
        />

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

        <EnvironmentPicker
          key={repo}
          repo={temporary ? '' : repo}
          temporary={temporary}
          open={environmentOpen}
          onClose={() => setEnvironmentOpen(false)}
          onChange={setEnvironmentSelection}
          onStatusChange={setEnvironmentStatus}
          onSummaryChange={setEnvironmentSummary}
        />
        <div className="flex min-w-0 flex-nowrap items-center gap-1 rounded-b-xl border-t border-border bg-surface-muted/50 p-2">
          <ComposerPlusMenu
            disabled={busy}
            onOpen={goalCommand.dismiss}
            onFiles={attachments.add}
            onOpenSkills={() => setSkillsOpen(true)}
            skillSummary={
              pendingSkillDefaults ? t('attach.skillsDefaults') : String(selectedSkills.length)
            }
            goalSummary={t('commands.goalHint')}
            onOpenGoal={insertGoalCommand}
            mcp={mcp}
            mcpBlockedReason={mcpBlockedReason}
            onOpenPlugins={
              pluginsSupported || plugins.length ? () => setPluginsOpen(true) : undefined
            }
            pluginSummary={String(plugins.length)}
            onOpenEnvironment={repo ? () => setEnvironmentOpen(true) : undefined}
            environmentSummary={environmentSummary}
          />
          <span aria-hidden="true" className="mx-0.5 h-4 w-px shrink-0 bg-border" />
          {/* Leave the scroller unpositioned so the repository menu anchors
                  to the composer and can extend outside the scroll area. */}
          <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [&>button]:shrink-0 [&>button]:whitespace-nowrap [&>label]:shrink-0 [&>label]:whitespace-nowrap">
            <RepositoryPicker
              repos={selectableRepos(config)}
              value={repo}
              onChange={chooseRepository}
              open={repoPickerOpen}
              onOpenChange={(next) => {
                if (next) openRepositoryPicker(false);
                else setRepoPickerOpen(false);
              }}
              onRefresh={() => void refreshRepositories()}
              onConfigure={config?.github_install_url ? () => void goToGitHub('manage') : undefined}
              onCreate={() => {
                if (!rewriting && !submitting) setCreateRepositoryOpen(true);
              }}
              // Repository access is GitHub's to give; until it is connected
              // the picker offers the connection instead of managing any.
              disconnected={disconnected}
              onConnect={canConnect ? () => void goToGitHub('connect') : undefined}
              refreshing={refreshing}
              createDisabled={rewriting || submitting}
              returnFocusTo={repoPickerFromTask ? taskBox : undefined}
            />
            {temporary ? null : (
              <Picker
                label={t('composer.branch')}
                value={branch}
                options={branches}
                onChange={setBranch}
              />
            )}
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
          </div>

          <button
            type="button"
            disabled={!canRun}
            onClick={() => void run()}
            title={missingRepository ? t('composer.chooseRepository') : t('composer.runTitle')}
            className="ui-button ui-button-primary ml-auto shrink-0 px-3 text-xs"
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

      {pluginsBlocked ? (
        <p role="alert" className="mt-3 text-center text-sm text-status-error">
          {pluginBlockedReason}{' '}
          <button type="button" className="underline" onClick={() => setPluginsOpen(true)}>
            {t('plugins.manage')}
          </button>
        </p>
      ) : null}

      {disconnected && canConnect ? (
        // Never in the way: a task runs without it, in a temporary workspace.
        <p className="mt-3 text-center text-xs leading-5">
          <button
            type="button"
            onClick={() => void goToGitHub('connect')}
            disabled={leaving === 'connect'}
            className="ui-focus-ring rounded font-medium text-accent hover:underline disabled:cursor-wait disabled:opacity-70"
          >
            {leaving === 'connect' ? t('connect.leaving') : t('temporaryWorkspace.connect')}
          </button>
        </p>
      ) : null}

      <p id={keyboardHintId} className="mt-3 text-center text-xs leading-5 text-muted">
        {t('composer.keyboardHint')}
      </p>

      {userId ? (
        <UserHomeInsights
          view={userProfile.view}
          loading={userProfile.loading}
          error={userProfile.error}
          onRetry={userProfile.retry}
        />
      ) : null}

      {pluginsOpen ? (
        <Suspense
          fallback={
            <ComposerDialogLoading
              title={t('plugins.title')}
              onClose={() => setPluginsOpen(false)}
            />
          }
        >
          <PluginSelectionDialog
            runtime={runtime}
            selected={plugins}
            onChange={setPlugins}
            onClose={() => setPluginsOpen(false)}
            blockedReason={pluginBlockedReason}
          />
        </Suspense>
      ) : null}
      {skillsOpen ? (
        <Suspense
          fallback={
            <ComposerDialogLoading title={t('skills.title')} onClose={() => setSkillsOpen(false)} />
          }
        >
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
        </Suspense>
      ) : null}
      {confirmOpen ? (
        <TemporaryWorkspaceConfirm
          returnFocus={taskBox}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            acknowledgement.acknowledge();
            setConfirmOpen(false);
            void run(true);
          }}
        />
      ) : null}
      <DeferredCreateRepositoryDialog
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
        <div className="mt-6 grid gap-3 border-t border-border pt-4 sm:grid-cols-3">
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
