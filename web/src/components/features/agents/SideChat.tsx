'use client';

import { memo, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';

import {
  cancelAgentJob,
  followUpAgentJob,
  type AgentConfigApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { useT } from '@/components/providers/LocaleProvider';
import { hasLostContext, latestAttempt, latestJob, ownerRunNote } from './conversationJob';
import { sideChatKeys } from './sideChatStorage';
import {
  composerModels,
  composerOverrides,
  ComposerTextarea,
  ComposerSendButton,
} from './ConversationComposer';
import { HistoryTurn } from './ChatMessages';
import { describeConversationTurns } from './conversationTurnNavigation';
import { highestEffort, offeredEfforts } from './effort';
import { McpApprovalNotice } from './mcp/McpApprovalNotice';
import { McpSessionPanel } from './mcp/McpSessionPanel';
import { Picker } from './Picker';
import { BackToBottom } from './TurnNavigator';
import type { AgentJob } from './types';
import { useAgentJob } from './useAgentJobs';
import { markConversationRead } from './useConversationReadState';
import { useConversationScroll } from './useConversationScroll';
import { useSideChatSession } from './useSideChatSession';

interface SideChatProps {
  sourceJob: AgentJob;
  sourceJobs: AgentJobApi[];
  userId?: string;
  config: AgentConfigApi | null;
  active: boolean;
}

function SideChatBoundary(props: SideChatProps) {
  // A main follow-up keeps this instance; navigating to another conversation
  // or switching accounts cannot reuse its draft, stream, or pending request.
  return (
    <SideChatContent
      key={`${props.userId ?? 'signed-out'}:${props.sourceJob.threadId ?? props.sourceJob.id}`}
      {...props}
    />
  );
}
export const SideChat = memo(SideChatBoundary);

function SideChatContent({
  sourceJob,
  sourceJobs,
  userId,
  config,
  active: selected,
}: SideChatProps) {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== 'hidden');
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);
  const active = selected && visible;
  const t = useT();
  const { session, status, error, recoverable, retry, restart, advance } = useSideChatSession(
    sourceJob,
    userId,
    active,
    sourceJobs,
  );
  const {
    job,
    threadJobs,
    loading,
    error: loadError,
    reload,
  } = useAgentJob(session?.jobId ?? '', { active, hideForkedHistory: true });
  const draftKey = userId ? sideChatKeys(userId, sourceJob.threadId ?? sourceJob.id).draft : '';
  const [draft, setDraft] = useState(() => {
    try {
      if (!draftKey) return '';
      const legacy = `agent-side-chat:${userId}:${sourceJob.threadId ?? sourceJob.id}:draft`;
      const value = sessionStorage.getItem(draftKey) ?? sessionStorage.getItem(legacy) ?? '';
      sessionStorage.removeItem(legacy);
      return value;
    } catch {
      return '';
    }
  });
  const [model, setModel] = useState(sourceJob.model);
  const [effort, setEffort] = useState(sourceJob.reasoningEffort ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toolsJobId, setToolsJobId] = useState<string | null>(null);
  const toolsOpen = toolsJobId !== null && toolsJobId === session?.jobId;
  const pickedModel = useRef(false);
  const pickedEffort = useRef(false);
  const modelInitialized = useRef(false);
  const submission = useRef(false);
  const stopRequested = useRef(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!draftKey) return;
    try {
      if (draft) sessionStorage.setItem(draftKey, draft);
      else sessionStorage.removeItem(draftKey);
    } catch {
      // Switching tabs still preserves the in-memory draft.
    }
  }, [draft, draftKey]);

  const belongsToSide = Boolean(
    session &&
    job &&
    job.threadId === session.threadId &&
    job.sandboxId === session.sandboxId &&
    job.sandboxId === sourceJob.sandboxId,
  );
  const current = belongsToSide && job?.id === session?.jobId ? job : null;
  const ready = Boolean(status === 'ready' && current && !loading && !loadError);
  const running = current?.state === 'running' || current?.state === 'queued';
  const unavailable = Boolean(current?.archivedAt || (current && hasLostContext(current)));

  useEffect(() => {
    if (!current || modelInitialized.current) return;
    modelInitialized.current = true;
    if (!pickedModel.current) setModel(current.model);
    if (!pickedEffort.current && !pickedModel.current) setEffort(current.reasoningEffort ?? '');
  }, [current]);

  const efforts = useMemo(
    () => offeredEfforts(config, current?.runtime ?? sourceJob.runtime, model || sourceJob.model),
    [config, current?.runtime, model, sourceJob.runtime, sourceJob.model],
  );
  useEffect(() => {
    if (config && !efforts.includes(effort)) {
      setEffort(highestEffort(efforts));
    }
  }, [config, effort, efforts]);
  const models = config?.models?.length
    ? composerModels(config.models, current?.model ?? sourceJob.model)
    : [];

  useEffect(() => {
    if (running) return;
    stopRequested.current = false;
    setStopping(false);
  }, [running]);

  // A refresh/return can find turns submitted through the child's ordinary
  // project-list entry. Follow that same conversation, never the source's.
  useEffect(() => {
    if (!ready || !session) return;
    const latest = latestJob(threadJobs);
    if (latest && latest.id !== session.jobId) {
      try {
        if (!advance(latest)) setActionError(t('sideChat.invalidChild'));
      } catch (cause) {
        setActionError(cause instanceof Error ? cause.message : t('sideChat.openFailed'));
      }
    }
  }, [advance, ready, session, threadJobs, t]);

  const turns = useMemo(() => {
    if (!belongsToSide || !job) return [];
    // Copied turns remain in the native child and the server's history. Only
    // this pane hides them: its blank initial body is presentation, not a
    // replacement prompt or a second implementation of session continuity.
    const own = new Set(
      threadJobs
        .filter((row) => row.thread_id === session?.threadId && !row.forked_from_job_id)
        .map((row) => row.id),
    );
    return describeConversationTurns(job, { attemptNo: latestAttempt(job) })
      .filter((turn) => own.has(turn.jobId))
      .map((turn, index) => ({ ...turn, displayNo: index + 1 }));
  }, [belongsToSide, job, session?.threadId, threadJobs]);
  const turnKeys = useMemo(() => turns.map((turn) => turn.key), [turns]);
  const scroll = useConversationScroll({
    conversationKey: session?.threadId ?? 'side-chat-preparing',
    turnKeys,
    active: active && !toolsOpen,
  });

  const readJob = useRef(current);
  readJob.current = current;
  useEffect(() => {
    const current = readJob.current;
    if (!active || toolsOpen || !ready || !current || !userId) return;
    markConversationRead(
      {
        key: current.threadId ?? current.id,
        job: current,
        jobIds: [current.id],
        archived: Boolean(current.archivedAt),
        lastActivityAt: null,
      },
      userId,
    );
  }, [
    active,
    current?.id,
    current?.state,
    current?.conversationState,
    current?.currentAttemptId,
    ready,
    toolsOpen,
    userId,
  ]);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = draft.trim();
    if (!ready) {
      setActionError(t('sideChat.preparing'));
      return;
    }
    if (!current || !prompt || unavailable || submission.current) return;
    submission.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      const next = await followUpAgentJob(current.id, {
        prompt,
        ...composerOverrides(current, model, effort),
      });
      if (!mounted.current) return;
      setDraft('');
      if (!advance(next)) {
        setActionError(t('sideChat.sentRestoreFailed'));
        reload();
      }
      scroll.followLatest();
    } catch (cause) {
      if (mounted.current) {
        setActionError(cause instanceof Error ? cause.message : t('sideChat.sendFailed'));
      }
    } finally {
      submission.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  const stop = useCallback(async () => {
    if (!current || !running || stopRequested.current) return;
    stopRequested.current = true;
    setStopping(true);
    setActionError(null);
    try {
      await cancelAgentJob(current.id);
      if (mounted.current) reload();
    } catch (cause) {
      stopRequested.current = false;
      if (mounted.current) {
        setStopping(false);
        setActionError(cause instanceof Error ? cause.message : t('sideChat.stopFailed'));
      }
    }
  }, [current, running, reload, t]);

  useEffect(() => {
    if (!active) return;
    const onEscape = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const ownsFocus =
        target === document.body ||
        target?.closest('#workspace-tab-side-chat, #workspace-panel-side-chat');
      if (
        event.key !== 'Escape' ||
        event.repeat ||
        event.defaultPrevented ||
        !ownsFocus ||
        target?.closest('input, textarea, select, [contenteditable="true"]')
      )
        return;
      event.preventDefault();
      void stop();
    };
    window.addEventListener('keydown', onEscape, true);
    return () => window.removeEventListener('keydown', onEscape, true);
  }, [active, stop]);
  useEffect(() => {
    const clear = (event: Event) => {
      if ((event as CustomEvent).detail === userId) setDraft('');
    };
    window.addEventListener('agents:side-chat-drafts-cleared', clear);
    return () => window.removeEventListener('agents:side-chat-drafts-cleared', clear);
  }, [userId]);
  const failure = error ?? loadError ?? actionError;
  return (
    <section
      aria-label={t('sideChat.region')}
      onKeyDown={(event) => {
        if (
          event.key !== 'Escape' ||
          event.repeat ||
          event.defaultPrevented ||
          (event.target instanceof Element &&
            event.target.closest('input, textarea, select, [contenteditable="true"]'))
        )
          return;
        event.preventDefault();
        void stop();
      }}
      className="flex h-full min-h-0 flex-col"
    >
      {toolsOpen && current ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <button
            type="button"
            onClick={() => setToolsJobId(null)}
            className="mb-4 text-sm text-gray-600 hover:underline dark:text-gray-400"
          >
            {t('sideChat.back')}
          </button>
          <McpSessionPanel jobId={current.id} threadId={current.threadId!} active={active} />
        </div>
      ) : (
        <div
          ref={scroll.containerRef}
          role="region"
          aria-label={t('sideChat.messages')}
          tabIndex={0}
          className="flex min-h-0 flex-1 flex-col overflow-y-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40"
        >
          <div className="flex-1 px-5 pt-3">
            {active && turns.map((turn) => <HistoryTurn key={turn.key} turn={turn} busy={false} />)}
            {turns.length && current?.liveNote ? (
              <p role="status" className="py-2 text-xs text-gray-500 dark:text-gray-400">
                {current.liveNote}
              </p>
            ) : null}
            {turns.length && current && ownerRunNote(current) ? (
              <p className="py-2 text-xs text-gray-500 dark:text-gray-400">
                {ownerRunNote(current!)}
              </p>
            ) : null}
          </div>
          <div ref={scroll.composerRef} className="sticky bottom-0 bg-white p-3 dark:bg-gray-900">
            {scroll.awayFromBottom ? (
              <div className="mb-2 flex justify-end">
                <BackToBottom
                  hasNewContent={scroll.hasNewContent}
                  onReturn={scroll.returnToBottom}
                />
              </div>
            ) : null}
            {current ? (
              <McpApprovalNotice
                jobId={current.id}
                active={active && running}
                onReview={() => setToolsJobId(current.id)}
              />
            ) : null}
            <form
              onSubmit={(event) => void send(event)}
              className="rounded-xl border border-gray-200 bg-white focus-within:border-crimson/30 dark:border-white/10 dark:bg-gray-900"
            >
              <ComposerTextarea
                aria-label={t('sideChat.message')}
                placeholder={t('sideChat.message')}
                rows={2}
                value={draft}
                disabled={unavailable || !userId}
                onChange={(event) => setDraft(event.target.value)}
                className="w-full resize-none rounded-t-xl border-0 bg-transparent px-4 pt-3 text-sm leading-relaxed text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 dark:text-gray-50 dark:placeholder:text-gray-500"
              />
              <div className="flex flex-wrap items-center gap-1 px-3 pb-2">
                {models.length ? (
                  <>
                    <Picker
                      label={t('sideChat.model')}
                      value={model}
                      options={models}
                      onChange={(value) => {
                        pickedModel.current = true;
                        setModel(value);
                      }}
                    />
                    <Picker
                      label={t('sideChat.effort')}
                      value={effort}
                      options={efforts}
                      onChange={(value) => {
                        pickedEffort.current = true;
                        setEffort(value);
                      }}
                    />
                  </>
                ) : (
                  <span className="min-w-0 flex-1 truncate text-xs text-gray-500 dark:text-gray-400">
                    {model}
                  </span>
                )}
                {running ? (
                  <>
                    <span className="text-xs text-gray-400 dark:text-gray-500">
                      {t('sideChat.queued')}
                    </span>
                    <button
                      type="button"
                      aria-label={t('sideChat.stop')}
                      disabled={stopping}
                      onClick={() => void stop()}
                      className="ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gray-900 text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
                    >
                      <span className="h-2.5 w-2.5 rounded-sm bg-current" />
                    </button>
                  </>
                ) : null}
                <ComposerSendButton
                  label={t('sideChat.send')}
                  title={t('sideChat.sendHint')}
                  busy={submitting || (active && status === 'preparing')}
                  disabled={!ready || unavailable || !draft.trim() || submitting}
                />
              </div>
            </form>
            {status === 'waiting' ? (
              <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                {t('sideChat.waiting')}
              </p>
            ) : null}
            {unavailable ? (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                {current?.archivedAt ? t('sideChat.archived') : t('sideChat.lost')}
              </p>
            ) : null}
            {unavailable && draft ? (
              <button type="button" onClick={() => setDraft('')} className="mt-2 text-xs underline">
                {t('sideChat.discard')}
              </button>
            ) : null}
            {failure ? (
              <div role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
                {failure.startsWith('sideChat.') ? t(failure as Parameters<typeof t>[0]) : failure}
                {recoverable ? (
                  <button type="button" onClick={() => void restart()} className="ml-2 underline">
                    {t('sideChat.restart')}
                  </button>
                ) : null}
                {error || loadError ? (
                  <button type="button" onClick={error ? retry : reload} className="ml-2 underline">
                    {t('sideChat.retry')}
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      )}
    </section>
  );
}
