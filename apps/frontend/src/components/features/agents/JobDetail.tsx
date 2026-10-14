'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import toast from 'react-hot-toast';
import { JobComposer } from './job-detail/JobComposer';
import { JobDetailHeader } from './job-detail/JobDetailHeader';
import { JobDetailSidePanels } from './job-detail/JobDetailSidePanels';
import { JobTranscript } from './job-detail/JobTranscript';
import { TrajectoryView } from './job-detail/LazyPanels';
import { useJobComposerSettings } from './job-detail/useJobComposerSettings';
import { useJobConversation } from './job-detail/useJobConversation';
import { useJobDetailPanels, type SubagentTarget } from './job-detail/useJobDetailPanels';
import { useJobRunActions } from './job-detail/useJobRunActions';

import { useT } from '@/components/providers';
import { WorkspaceLinkProvider } from '@/components/ui/Markdown';
import { basePath } from '@/config/env';
import { submitThreadMessage, type ThreadMessageResultApi } from '@/lib/api/pendingMessages';
import { APIError } from '@/lib/utils/errors';

import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import type { SelectedSkillApi } from '@/lib/api/skills';
import { ActivityWindow } from './ActivityWindow';
import { ownedBySubagent, subagentDisplayEvents } from './adapt';
import { SubagentLinks, type SubagentOfCall } from './ChatMessages';
import { composerOverrides } from './ConversationComposer';
import { FloatingActivityWindow } from './FloatingActivityWindow';
import { LedgerUsage, useLedgerUsage } from './LedgerUsage';
import { notDeliveredText } from './PendingMessageQueue';
import { SubagentConversation } from './SubagentConversation';
import { BackToBottom, TurnRail } from './TurnNavigator';
import {
  hasActivityOverview,
  subagentStartedBy,
  turnActivityOverview,
  type OverviewSubagent,
} from './activityOverview';
import { useAttachments } from './attachments/useAttachments';
import { codeReviewPrompt } from './codeReview';
import { composerCommandMatches } from './composerCommands';
import { hasLostContext, isActiveConversationState, isBusyJob, latestJob } from './conversationJob';
import { useFirstRun } from './firstRun';
import { isGoalRevisionConflict, writeGoal } from './goal/goalApi';
import { hasLiveWork } from './goal/liveWork';
import { GOAL_COMMAND_PREFIX, useGoalCommand } from './goal/useGoalCommand';
import { useThreadComposerMcp } from './mcp/useThreadComposerMcp';
import { createRecordedOutputsSelector } from './recordedTurnOutputs';
import { useTranscriptPresentation } from './transcriptPresentation';
import type { AgentJob } from './types';
import { useComposerDraft } from './useComposerDraft';
import { usePendingMessages } from './usePendingMessages';
import { watchPullRequestPrompt, watchPullRequestRequestId } from './watchPullRequest';

export {
  INSPECTOR_WIDTH_STORAGE_KEY,
  WORKSPACE_WIDTH_STORAGE_KEY,
} from './job-detail/useJobDetailPanels';

const EMPTY_STORED_EVENTS: (AgentJobEventApi | AgentThreadEventApi)[] = [];
const EMPTY_THREAD_JOBS: AgentJobApi[] = [];

// The rail needs room to sit beside the text rather than over it, and a band
// tall enough that its marks mean something. Otherwise the transcript keeps
// its normal scrolling, without adding a second navigation surface.
const RAIL_MIN_COLUMN_WIDTH = 720;
const RAIL_MIN_READING_HEIGHT = 200;
const RAIL_MAX_HEIGHT = 420;
const RAIL_BAND_MARGIN = 64;
const BACK_TO_BOTTOM_INSET = 16;

/** Coordinates conversation requests and composes the focused job-detail views. */
export function JobDetail({
  job,
  userId,
  events = EMPTY_STORED_EVENTS,
  threadJobs = EMPTY_THREAD_JOBS,
  onReload,
}: {
  job: AgentJob;
  userId?: string;
  /** The stored stream, for the trajectory tab. Absent in fixtures. */
  events?: (AgentJobEventApi | AgentThreadEventApi)[];
  /** The conversation's turn rows, so Run details can describe a past turn. */
  threadJobs?: AgentJobApi[];
  onReload?: () => void;
}) {
  const t = useT();
  const usage = useLedgerUsage(job);
  const threadId = job.threadId ?? job.id;
  const panels = useJobDetailPanels({ job, userId, events, threadJobs });
  const {
    tab,
    setTab,
    subagent,
    setSubagent,
    workspaceOpen,
    setWorkspaceOpen,
    workspaceTabs,
    activityOpen,
    setActivityOpen,
    activityToggleRef,
    inspector,
    setInspector,
    focusJobId,
    setFocusJobId,
    trajectoryQuery,
    setTrajectoryQuery,
    trajectorySubagent,
    setTrajectorySubagent,
    trajectorySteps,
    layoutContainerRef,
    openWorkspaceFile,
    selectStep,
    openRun,
  } = panels;
  const liveAttempt = useMemo(
    () => (job.attempts.length > 0 ? job.attempts[job.attempts.length - 1].no : 0),
    [job.attempts],
  );
  const [attemptNo, setAttemptNo] = useState(liveAttempt);
  // What the remount used to do. Moving to the next turn of a conversation no
  // longer rebuilds this component, so the attempt selection has to follow the
  // turn explicitly — otherwise turn two would open showing turn one's chosen
  // attempt.
  useEffect(() => {
    setAttemptNo(liveAttempt);
    // Only when the turn changes: re-running this on every attempt update
    // would fight the reader's own selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);
  const [title, setTitle] = useState(job.title);
  useEffect(() => setTitle(job.title), [job.title, job.threadId]);
  const draft = useComposerDraft({ userId, threadId });
  const { value: followUp, setValue: setFollowUp } = draft;
  const currentDraftScope = useRef(draft.capture().scope);
  currentDraftScope.current = draft.capture().scope;
  const composerMounted = useRef(true);
  useEffect(() => {
    composerMounted.current = true;
    return () => {
      composerMounted.current = false;
    };
  }, []);
  const [presentation, setPresentation] = useTranscriptPresentation(userId, threadId);
  const recordedOutputsSelector = useMemo(() => createRecordedOutputsSelector(), []);
  const recordedOutputs = useMemo(
    () => recordedOutputsSelector.select(threadJobs, events),
    [recordedOutputsSelector, threadJobs, events],
  );
  const [chosenSkills, setChosenSkills] = useState<SelectedSkillApi[] | null>(null);
  // What to say when `/goal` was typed with nothing after it. Guidance, not a
  // failure: nothing was created and nothing was sent.
  const [goalHint, setGoalHint] = useState<string | null>(null);
  const followUpBox = useRef<HTMLTextAreaElement>(null);
  const [readingFiles, setReadingFiles] = useState(false);
  // Follow-ups append to the thread's latest turn, even from an older URL, and
  // an omitted skill set, model or effort is inherited from that turn.
  const newestTurn = latestJob(threadJobs);
  const parentTurn =
    newestTurn && (newestTurn.turn_no ?? 1) > (job.turnNo ?? 1) ? newestTurn : undefined;
  const inheritedSkills = parentTurn ? (parentTurn.skills ?? []) : (job.skills ?? []);
  const inherited = {
    model: parentTurn ? parentTurn.model : job.model,
    reasoningEffort: parentTurn ? (parentTurn.reasoning_effort ?? undefined) : job.reasoningEffort,
  };
  // Unsent composer choices belong to the conversation, including when a PR
  // action advances it to another turn without sending the owner's draft.
  // The composer settings reset the chosen model and effort by the same rule.
  useEffect(() => setChosenSkills(null), [threadId]);
  // The same rule for the goal guidance. This component is deliberately not
  // remounted when the conversation moves to its next turn, so anything that
  // belongs to *one* message has to be reset explicitly when that turn does.
  useEffect(() => setGoalHint(null), [job.id]);
  const settings = useJobComposerSettings(job, inherited);
  const { config, model, effort } = settings;
  const attachments = useAttachments(t, config?.attachment_limits);
  const mcp = useThreadComposerMcp(job.threadId);
  const selectedSkills = chosenSkills ?? inheritedSkills;
  const mcpBlockedReason =
    config?.mcp_runtimes && !config.mcp_runtimes.includes(job.runtime)
      ? t('attach.mcpUnsupported', { runtime: job.runtime })
      : undefined;
  const [submitting, setSubmitting] = useState(false);
  const [rewriting, setRewriting] = useState(false);
  const sendRequestedRef = useRef(false);
  const watchPrRequestedRef = useRef(false);
  const [watchPrAcceptedJobId, setWatchPrAcceptedJobId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const runActions = useJobRunActions({ job, userId, onReload, onError: setActionError });
  const { stopping, forking, stop, forkFrom, canForkWorkspace, canForkCurrent, canReview } =
    runActions;
  // The goal this conversation already has, as the page last read it. Every
  // goal write travels with this revision, so an edit made in another tab is
  // refused rather than silently overwritten.
  const currentGoal = job.goal ?? null;
  // The first turn of the owner's first conversation explains itself as it
  // goes, and says where its draft PR is when it has one.
  const { onlyThreadId } = useFirstRun();
  const firstRun = (job.turnNo ?? 1) === 1 && onlyThreadId === threadId;
  const goalCommand = useGoalCommand({
    value: followUp,
    setValue: setFollowUp,
    focusComposer: () => followUpBox.current?.focus(),
    onNeedsObjective: () => setGoalHint(t('goal.needsObjective')),
    onApply: applyGoalObjective,
    onError: setActionError,
  });
  useEffect(() => setAttemptNo(liveAttempt), [liveAttempt]);

  // Edit & rewind creates the native child immediately, then leaves the
  // replacement prompt as a one-shot draft on that child's landing page.
  useEffect(() => {
    const key = `agent-rewind-draft-${job.id}`;
    let draft: string | null = null;
    try {
      draft = sessionStorage.getItem(key);
      if (draft) sessionStorage.removeItem(key);
    } catch {
      return;
    }
    if (draft) setFollowUp(draft);
  }, [job.id, setFollowUp]);

  const isArchived = Boolean(job.archivedAt);
  const isActive = job.state === 'running' || job.state === 'queued';
  const isSuccessful = job.state === 'done' || job.state === 'needs_review';
  // `contextStatus` describes the selected attempt. `nativeState` is the
  // current membership truth, so it also closes the composer when someone is
  // viewing an older successful turn after a later admitted turn was lost.
  const contextLost = hasLostContext(job);
  const latestQuestionTurn = latestJob(
    threadJobs.filter((turn) => !turn.thread_id || turn.thread_id === threadId),
  );
  let questionTurn = {
    id: job.id,
    attemptId: job.currentAttemptId,
    active: job.state === 'running' && !contextLost,
  };
  if (latestQuestionTurn && (latestQuestionTurn.turn_no ?? 1) > (job.turnNo ?? 1)) {
    questionTurn = {
      id: latestQuestionTurn.id,
      attemptId: latestQuestionTurn.current_attempt_id,
      active:
        latestQuestionTurn.state === 'running' &&
        !latestQuestionTurn.cancel_requested &&
        latestQuestionTurn.native_state !== 'lost',
    };
  }
  const latestTerminalPhase = useMemo(() => {
    for (let index = job.events.length - 1; index >= 0; index -= 1) {
      const event = job.events[index];
      if (
        event.kind === 'lifecycle' &&
        event.attemptNo === job.currentAttemptNo &&
        (event.text === 'workspace_preparing' ||
          event.text === 'workspace_ready' ||
          event.text === 'workspace_finalizing')
      ) {
        return event.text;
      }
    }
    return undefined;
  }, [job.events, job.currentAttemptNo]);
  const terminalWorkspaceReady =
    !isActive ||
    (job.state === 'running' &&
      job.currentAttemptNo !== undefined &&
      latestTerminalPhase === 'workspace_ready');

  useEffect(() => setWatchPrAcceptedJobId(null), [job.id]);

  /**
   * The plus menu's Goal row: put `/goal ` in the box and leave the caret after
   * it. Nothing opens, because there is nothing left to open — the band above
   * the messages is where a goal is read and managed, and this row is how the
   * sentence that sets one gets started.
   */
  function insertGoalCommand() {
    setGoalHint(null);
    setFollowUp((current) => {
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
    followUpBox.current?.focus();
  }

  /**
   * `/goal <objective>` on a conversation that already exists.
   *
   * The thread's own endpoint, not a message: the owner's set *is* the
   * authorization, and the platform schedules the first round from it, so
   * nothing has to be sent that says "begin". A refusal is thrown rather than
   * drawn here — the command reports it and keeps the text, because the retry
   * is the same command.
   */
  async function applyGoalObjective(objective: string): Promise<void> {
    try {
      // No round bound travels with it: the goal the owner asked for is
      // bounded by nothing. A bound the conversation already carries is a
      // choice somebody made elsewhere, and replacing the objective does not
      // silently restate it — nor silently drop it, which is why the field is
      // simply absent here rather than echoed back.
      await writeGoal(threadId, objective, currentGoal?.revision);
    } catch (cause) {
      // The revision this page held is the stale thing the server just
      // refused, so the conversation has to be read back before the owner
      // tries again. The refusal itself still travels: the command keeps the
      // text, and the retry is the same command.
      if (isGoalRevisionConflict(cause)) onReload?.();
      throw cause;
    }
    setGoalHint(null);
    setActionError(null);
    onReload?.();
  }

  async function submitFollowUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendDraft();
  }

  /** Send what the composer holds; the result, or `null` if nothing went. */
  async function sendDraft(): Promise<ThreadMessageResultApi | null> {
    if (
      sendRequestedRef.current ||
      rewriting ||
      watchPrRequestedRef.current ||
      contextLost ||
      isArchived ||
      attachments.uploading ||
      readingFiles ||
      mcp.saving
    )
      return null;
    const snapshot = draft.capture();
    const prompt = snapshot.value.trim();
    if (!prompt) return null;
    sendRequestedRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      // Commands use their own operation and are never queued as messages.
      if (await goalCommand.submit()) return null;
      // One request whatever the conversation is doing: idle, the message is
      // the next turn when this returns; busy, it waits in the queue above the
      // composer. ``queue.apply`` shows the card or, for a turn, moves to it.
      const result = await submitThreadMessage(threadId, {
        prompt,
        ...(chosenSkills !== null ? { skill_ids: chosenSkills.map((skill) => skill.id) } : {}),
        ...(attachments.ids.length ? { attachment_ids: attachments.ids } : {}),
        // Omitted controls inherit the parent's validated model and effort.
        // The parent is the latest turn, which need not be the one on screen.
        ...composerOverrides(inherited, model, effort),
      });
      draft.acknowledge(snapshot);
      if (!composerMounted.current || currentDraftScope.current !== snapshot.scope) return result;
      queue.apply(result);
      // The queued message retains its uploads; the next draft starts without them.
      setChosenSkills(null);
      attachments.reset();
      return result;
    } catch (cause: unknown) {
      if (!composerMounted.current || currentDraftScope.current !== snapshot.scope) return null;
      setActionError(cause instanceof Error ? cause.message : 'Could not send the message');
      return null;
    } finally {
      sendRequestedRef.current = false;
      if (composerMounted.current) setSubmitting(false);
    }
  }

  /**
   * Cmd/Ctrl+Enter while a turn that can take it is live: the draft goes into
   * that turn, before the model's next tool call, instead of after it. Only the
   * draft: what already waits was left for after the turn, and each has its
   * own ↑ for changing that.
   */
  async function steerNow() {
    const scope = draft.capture().scope;
    const drafted = followUp.trim() ? await sendDraft() : null;
    if (!composerMounted.current || currentDraftScope.current !== scope) return;
    // Nothing to add, or nothing running: the draft simply became the next turn.
    if (!drafted?.pending || (drafted.pending.attachments ?? []).length) return;
    const sent = await queue.sendNow(drafted.pending);
    if (!composerMounted.current || currentDraftScope.current !== scope) return;
    if (!sent.ok) {
      setActionError(sent.error);
      return;
    }
    const reason = sent.result.not_delivered_reason;
    if (reason) toast(notDeliveredText(reason, t));
  }

  function openReview() {
    if (!canReview) return;
    workspaceTabs.review(codeReviewPrompt(job, threadJobs));
    setWorkspaceOpen(true);
    setInspector(null);
  }
  const selectedAttemptIsCurrent = attemptNo === 0 || attemptNo === liveAttempt;
  const usageFooter = selectedAttemptIsCurrent ? (
    <LedgerUsage job={job} usage={usage} />
  ) : undefined;

  const conversation = useJobConversation({ job, panels, attemptNo, liveAttempt, setAttemptNo });
  const {
    historyTurns,
    currentTurn,
    rewindAnchors,
    currentRewindAnchor,
    visibleEvents,
    navigationEntries,
    scroll,
    jumpToTurn,
    navigationNotice,
    selectAttempt,
  } = conversation;
  const activityOverview = useMemo(() => turnActivityOverview(visibleEvents), [visibleEvents]);
  const hasActivity = hasActivityOverview(activityOverview);
  const activityVisible = activityOpen && hasActivity && !workspaceOpen && inspector === null;
  const closeActivity = useCallback(() => {
    setActivityOpen(false);
    activityToggleRef.current?.focus();
  }, [activityToggleRef, setActivityOpen]);
  const toggleActivity = useCallback(() => {
    if (activityVisible) {
      closeActivity();
      return;
    }
    setWorkspaceOpen(false);
    setInspector(null);
    setFocusJobId(null);
    setActivityOpen(true);
  }, [
    activityVisible,
    closeActivity,
    setWorkspaceOpen,
    setActivityOpen,
    setFocusJobId,
    setInspector,
  ]);
  // A subagent is named by the turn that started it, so any turn's can be
  // opened. One that is no longer shown — another attempt was selected —
  // returns to the Chat it replaced.
  const turnOf = useCallback(
    (jobId: string) =>
      jobId === currentTurn.jobId ? currentTurn : historyTurns.find((turn) => turn.jobId === jobId),
    [currentTurn, historyTurns],
  );
  const subagentAt = useCallback(
    (target: SubagentTarget | null) => {
      const turn = target === null ? undefined : turnOf(target.jobId);
      return turn === undefined
        ? undefined
        : turnActivityOverview(turn.events).subagents.find((agent) => agent.key === target?.key);
    },
    [turnOf],
  );
  // Codex threads outlive a turn; read only the turn and attempt that started one.
  const subagentScope = useCallback(
    (jobId: string, agent: OverviewSubagent) => {
      const source = events.find((event) => event.id === agent.sourceEvent.sourceEventId);
      return {
        inScope: (event: AgentJobEventApi | AgentThreadEventApi) =>
          (!('job_id' in event) || event.job_id === jobId) &&
          (!source || event.attempt_id === source.attempt_id),
        owner: { callId: agent.sourceEvent.id, threads: agent.activity.targets },
      };
    },
    [events],
  );
  const openSubagent = subagentAt(subagent);
  const openSubagentJobId = openSubagent ? subagent?.jobId : undefined;
  useEffect(() => {
    if (subagent !== null && !openSubagent) setSubagent(null);
  }, [openSubagent, setSubagent, subagent]);
  const subagentEvents = useMemo(() => {
    if (!openSubagent || !openSubagentJobId) return [];
    const { inScope, owner } = subagentScope(openSubagentJobId, openSubagent);
    return subagentDisplayEvents(
      events.filter(inScope),
      owner,
      threadJobs.find((turn) => turn.id === openSubagentJobId),
    );
  }, [events, openSubagent, openSubagentJobId, subagentScope, threadJobs]);
  const showingSubagent = tab === 'chat' && openSubagent !== undefined;
  const subagentOfCall = useCallback<SubagentOfCall>(
    (jobId, event) => {
      const turn = turnOf(jobId);
      const agent = turn && subagentStartedBy(turnActivityOverview(turn.events), event);
      if (!agent) return undefined;
      return {
        title: agent.activity.description ?? agent.activity.title,
        open: () => {
          setTab('chat');
          setSubagent({ jobId, key: agent.key });
        },
      };
    },
    [setSubagent, setTab, turnOf],
  );
  // Every turn's subagents, for narrowing the trajectory to one of them.
  const trajectorySubagents = useMemo(() => {
    if (tab !== 'trajectory') return [];
    return [...historyTurns, currentTurn].flatMap((turn) =>
      turnActivityOverview(turn.events).subagents.map((agent) => ({
        value: JSON.stringify([turn.jobId, agent.key]),
        label: `Turn ${turn.displayNo} · ${agent.activity.description ?? agent.activity.title}`,
      })),
    );
  }, [currentTurn, historyTurns, tab]);
  const trajectoryAgent = subagentAt(trajectorySubagent);
  const trajectoryAgentJobId = trajectoryAgent ? trajectorySubagent?.jobId : undefined;
  const shownTrajectorySteps = useMemo(() => {
    if (!trajectoryAgent || !trajectoryAgentJobId) return trajectorySteps;
    const { inScope, owner } = subagentScope(trajectoryAgentJobId, trajectoryAgent);
    // The call that started the subagent, then what the subagent recorded.
    return trajectorySteps.filter(
      (step) =>
        step.sourceEvents.some(inScope) &&
        ((step.callId !== undefined && step.callId === owner.callId) ||
          step.sourceEvents.some((event) => ownedBySubagent(event.payload, owner))),
    );
  }, [subagentScope, trajectoryAgent, trajectoryAgentJobId, trajectorySteps]);
  useEffect(() => {
    if (!activityVisible) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (
        event.key !== 'Escape' ||
        event.repeat ||
        event.defaultPrevented ||
        !target?.closest('#job-activity-window') ||
        // A maximized panel restores on Escape; only the floating one closes.
        target.closest('#job-activity-window[data-maximized]') ||
        target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      closeActivity();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [activityVisible, closeActivity, setWorkspaceOpen]);
  // The conversation's waiting messages. A message becomes a turn on the
  // server, when the turn in front of it ends; the page follows it there when
  // it asked for it, or when it was showing the turn the new one follows.
  const followTurn = useCallback(
    (jobId: string) => {
      // A router.push remounts the dynamic [jobId] segment even with
      // scroll:false and a stable JobDetail key. Native history updates Next's
      // usePathname without replacing the page, preserving the conversation
      // while useAgentJob loads the next turn. Reload and Back keep real URLs.
      window.history.pushState(null, '', `${basePath}/${encodeURIComponent(jobId)}`);
      // The reader asked for this turn, so following its output is what they
      // asked for — the one place besides "back to bottom" that resumes it.
      scroll.followLatest();
    },
    [scroll],
  );
  const viewedJobId = job.id;
  const queue = usePendingMessages({
    // The queue this page writes to is the one it reads.
    threadId,
    active: hasLiveWork(job),
    onSent: (jobId, how) => {
      if (jobId === viewedJobId) return;
      if (how.requested || how.after === viewedJobId) followTurn(jobId);
    },
  });
  const watchPrPrompt = watchPullRequestPrompt(job);
  // These live fields describe the entire conversation/workspace. Other
  // thread rows can retain older pending/running values after it settles.
  const publicationState =
    job.workspacePublicationState ?? latestQuestionTurn?.workspace_publication_state;
  const publicationPending = Boolean(publicationState && publicationState !== 'published');
  const conversationBusy =
    job.conversationState !== undefined
      ? isActiveConversationState(job.conversationState)
      : Boolean(latestQuestionTurn && isBusyJob(latestQuestionTurn));
  // An idle send resumes a paused queue oldest-first. This action must start
  // its own request, so wait until the queue is known to have no other work.
  const canWatchPr = Boolean(
    userId &&
    job.prUrl &&
    isSuccessful &&
    !isActive &&
    !isArchived &&
    !contextLost &&
    !submitting &&
    !rewriting &&
    !mcp.saving &&
    !publicationPending &&
    watchPrAcceptedJobId !== job.id &&
    queue.loaded &&
    queue.latestJobId &&
    ['succeeded', 'failed', 'cancelled'].includes(queue.latestJobState ?? '') &&
    queue.messages.length === 0 &&
    !queue.resuming &&
    !conversationBusy,
  );

  async function watchPr() {
    const latestJobId = queue.latestJobId;
    if (!canWatchPr || !latestJobId || watchPrRequestedRef.current || sendRequestedRef.current)
      return;
    const watchScope = currentDraftScope.current;
    watchPrRequestedRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      // This action sends its own message. Unsent text, uploads and composer
      // selections belong to the owner's next message and stay in place.
      // The server replays this receipt under the thread lock, including for
      // another tab viewing a different turn of the same conversation. Fresh
      // requests refuse atomically if its queue or published boundary changes.
      const result = await submitThreadMessage(
        threadId,
        { prompt: watchPrPrompt, require_idle: true, expected_latest_job_id: latestJobId },
        { requestId: watchPullRequestRequestId(threadId, latestJobId) },
      );
      if (!composerMounted.current || currentDraftScope.current !== watchScope) return;
      // Keep the completed source action closed while the next turn loads.
      if (result.job && !result.pending) setWatchPrAcceptedJobId(job.id);
      queue.apply(result);
    } catch (cause: unknown) {
      if (!composerMounted.current || currentDraftScope.current !== watchScope) return;
      if (cause instanceof APIError && cause.code === 'conversation_changed') void queue.refresh();
      setActionError(cause instanceof Error ? cause.message : t('watchPr.sendFailed'));
    } finally {
      watchPrRequestedRef.current = false;
      if (composerMounted.current) setSubmitting(false);
    }
  }

  const showRail =
    tab === 'chat' &&
    !showingSubagent &&
    !activityVisible &&
    navigationEntries.length > 1 &&
    scroll.viewport.width >= RAIL_MIN_COLUMN_WIDTH &&
    scroll.viewport.readingHeight >= RAIL_MIN_READING_HEIGHT &&
    scroll.viewport.finePointer;
  const composerHeight = scroll.viewport.composerHeight;

  // The workspace and inspector share the right-hand region.
  const panelOpen = workspaceOpen || inspector !== null;
  const content = (
    <section className="flex h-full min-h-0 flex-col bg-surface">
      <JobDetailHeader
        panels={panels}
        job={job}
        title={title}
        canForkCurrent={canForkCurrent}
        forking={forking}
        onFork={() => void forkFrom(job.id)}
        onRename={setTitle}
        presentation={presentation}
        onPresentationChange={(mode) => {
          scroll.holdReading();
          setPresentation(mode);
        }}
        hasActivity={hasActivity}
        activityVisible={activityVisible}
        activityToggleRef={activityToggleRef}
        toggleActivity={toggleActivity}
        stopping={stopping}
        onStop={() => void stop()}
      />

      <div
        ref={layoutContainerRef}
        className={`min-h-0 flex-1 overflow-hidden ${panelOpen ? 'block lg:flex' : 'flex'}`}
      >
        {/* The rail's positioning layer: a sibling of the scroller, so it never
            adds to the transcript's scrollHeight, and inside the Chat column,
            so it stays beside the conversation when the workspace opens. */}
        <div className="relative flex min-w-0 flex-1">
          <div
            ref={scroll.containerRef}
            role="region"
            aria-label="Task progress"
            // A scrollable region belongs in the tab order: without it the
            // transcript can only be scrolled by focusing something inside it,
            // and the one thing this feature must never do is move a reader
            // who is looking somewhere else.
            tabIndex={0}
            className={`min-w-0 flex-1 flex-col overflow-y-auto overscroll-y-none [overflow-anchor:none] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40 ${
              panelOpen ? 'hidden lg:flex' : 'flex'
            }`}
          >
            {/* The rail is an overlay, so the text gives back the width it
                occupies rather than running underneath the marks. The
                trajectory is a wide table of steps, so it keeps the width the
                inspector is not holding instead of a reading column. */}
            <div
              className={`flex w-full flex-1 flex-col pb-4 pt-4 ${
                tab === 'trajectory'
                  ? 'px-4'
                  : `mx-auto max-w-4xl ${showRail ? 'pl-4 pr-10' : 'px-4'}`
              }`}
            >
              {tab === 'trajectory' ? (
                <div className="flex-1">
                  <TrajectoryView
                    events={events}
                    jobs={threadJobs}
                    steps={shownTrajectorySteps}
                    query={trajectoryQuery}
                    onQueryChange={setTrajectoryQuery}
                    subagents={trajectorySubagents}
                    subagentFilter={
                      trajectoryAgent && trajectorySubagent
                        ? JSON.stringify([trajectorySubagent.jobId, trajectorySubagent.key])
                        : ''
                    }
                    onSubagentFilterChange={(value) => {
                      if (!value) {
                        setTrajectorySubagent(null);
                        return;
                      }
                      const [jobId, key] = JSON.parse(value) as [string, string];
                      setTrajectorySubagent({ jobId, key });
                    }}
                    selectedKey={inspector?.kind === 'step' ? inspector.stepKey : null}
                    onSelectStep={selectStep}
                    onOpenRun={openRun}
                    focusJobId={focusJobId}
                  />
                </div>
              ) : openSubagent ? (
                <SubagentConversation
                  agent={openSubagent}
                  events={subagentEvents}
                  jobId={openSubagentJobId ?? job.id}
                  presentation={presentation}
                  onBack={() => setSubagent(null)}
                  onViewTrajectory={() => {
                    setTrajectorySubagent(subagent);
                    setTab('trajectory');
                  }}
                  onInspectEvent={events.length > 0 ? panels.inspectTranscriptEvent : undefined}
                />
              ) : (
                <SubagentLinks.Provider value={subagentOfCall}>
                  <JobTranscript
                    panels={panels}
                    job={job}
                    historyTurns={historyTurns}
                    currentTurn={currentTurn}
                    presentation={presentation}
                    hasStoredEvents={events.length > 0}
                    rewindAnchors={rewindAnchors}
                    currentRewindAnchor={currentRewindAnchor}
                    canForkWorkspace={canForkWorkspace}
                    canForkCurrent={canForkCurrent}
                    forking={forking}
                    forkFrom={forkFrom}
                    attemptNo={attemptNo}
                    liveAttempt={liveAttempt}
                    onSelectAttempt={selectAttempt}
                    selectedAttemptIsCurrent={selectedAttemptIsCurrent}
                    usageFooter={usageFooter}
                    firstRun={firstRun}
                    recordedOutputs={recordedOutputs}
                    onReview={canReview ? openReview : undefined}
                    onWatchPr={canWatchPr ? () => void watchPr() : undefined}
                  />
                </SubagentLinks.Provider>
              )}

              <JobComposer
                job={job}
                settings={settings}
                runActions={runActions}
                draft={draft}
                attachments={attachments}
                mcp={mcp}
                queue={queue}
                goalCommand={goalCommand}
                composerRef={scroll.composerRef}
                followUpBox={followUpBox}
                questionTurn={questionTurn}
                goalHint={goalHint}
                setGoalHint={setGoalHint}
                selectedSkills={selectedSkills}
                setChosenSkills={setChosenSkills}
                mcpBlockedReason={mcpBlockedReason}
                submitting={submitting}
                rewriting={rewriting}
                readingFiles={readingFiles}
                setReadingFiles={setReadingFiles}
                setRewriting={setRewriting}
                actionError={actionError}
                setActionError={setActionError}
                submitFollowUp={submitFollowUp}
                steerNow={steerNow}
                insertGoalCommand={insertGoalCommand}
                onReload={onReload}
                hidden={showingSubagent}
              />
            </div>
          </div>

          <FloatingActivityWindow visible={activityVisible} bottom={composerHeight + 64}>
            {(moveHandleProps, maximize) => (
              <ActivityWindow
                moveHandleProps={moveHandleProps}
                maximize={maximize}
                overview={activityOverview}
                turnNo={currentTurn.displayNo}
                attemptNo={attemptNo}
                onClose={closeActivity}
                openSubagentKey={
                  showingSubagent && subagent?.jobId === currentTurn.jobId ? subagent.key : null
                }
                onOpenSubagent={(key) => {
                  setTab('chat');
                  setSubagent({ jobId: currentTurn.jobId, key });
                }}
              />
            )}
          </FloatingActivityWindow>

          {/* Above the transcript, below its composer and the page header:
              the design's order is body, rail preview, composer, header. */}
          {showRail ? (
            <div
              className="pointer-events-none absolute right-3 top-0 z-0 flex flex-col justify-center"
              style={{ bottom: composerHeight }}
            >
              <TurnRail
                entries={navigationEntries}
                activeKey={scroll.activeKey}
                onNavigate={jumpToTurn}
                maxHeightPx={Math.min(
                  RAIL_MAX_HEIGHT,
                  scroll.viewport.readingHeight - RAIL_BAND_MARGIN,
                )}
              />
            </div>
          ) : null}

          {tab === 'chat' && !showingSubagent ? (
            <div
              className="pointer-events-none absolute left-0 z-0 w-full max-w-full"
              style={{
                bottom: composerHeight + BACK_TO_BOTTOM_INSET,
                // Match the scroller's usable width, including when a native
                // scrollbar occupies space beside the conversation.
                width: scroll.viewport.width || undefined,
              }}
            >
              <div
                className={`mx-auto flex w-full max-w-4xl flex-col items-end gap-2 ${showRail ? 'pl-4 pr-10' : 'px-4'}`}
              >
                {/* One live region for both answers: where a jump landed, and
                  when it could not land at all. It is always in the DOM, since
                  a region added at the same moment as its text announces
                  nothing. */}
                <p
                  role="status"
                  aria-live="polite"
                  className={
                    scroll.unavailableKey === null
                      ? 'sr-only'
                      : 'rounded-full bg-white/95 px-3 py-1 text-xs text-gray-600 shadow-card dark:bg-gray-900/95 dark:text-gray-300'
                  }
                >
                  {navigationNotice}
                </p>
                {scroll.awayFromBottom ? (
                  <BackToBottom
                    hasNewContent={scroll.hasNewContent}
                    onReturn={scroll.returnToBottom}
                  />
                ) : null}
              </div>
            </div>
          ) : null}
        </div>

        <JobDetailSidePanels
          panels={panels}
          job={job}
          userId={userId}
          config={config}
          events={events}
          threadJobs={threadJobs}
          terminalWorkspaceReady={terminalWorkspaceReady}
          onReview={canReview ? openReview : undefined}
          onWatchPr={canWatchPr ? () => void watchPr() : undefined}
        />
      </div>
    </section>
  );
  return <WorkspaceLinkProvider onOpenFile={openWorkspaceFile}>{content}</WorkspaceLinkProvider>;
}
