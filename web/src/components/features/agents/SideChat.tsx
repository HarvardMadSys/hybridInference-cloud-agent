'use client';

import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';

import {
  cancelAgentJob,
  followUpAgentJob,
  type AgentConfigApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { ownerFailureMessage } from './adapt';
import { HistoryTurn } from './ChatMessages';
import { isComposerSubmit } from './composerKeys';
import { describeCurrentTurn, describeHistoryTurns } from './conversationTurnNavigation';
import { highestEffort, offeredEfforts } from './effort';
import { McpApprovalNotice } from './mcp/McpApprovalNotice';
import { McpSessionPanel } from './mcp/McpSessionPanel';
import { Picker } from './Picker';
import { BackToBottom } from './TurnNavigator';
import type { AgentJob } from './types';
import { useAgentJob } from './useAgentJobs';
import { markConversationRead } from './useConversationReadState';
import { useConversationScroll } from './useConversationScroll';
import { sideChatStorageKey, useSideChatSession } from './useSideChatSession';

interface SideChatProps {
  sourceJob: AgentJob;
  sourceJobs: AgentJobApi[];
  userId?: string;
  config: AgentConfigApi | null;
  active: boolean;
}

export function SideChat(props: SideChatProps) {
  // A main follow-up keeps this instance; navigating to another conversation
  // or switching accounts cannot reuse its draft, stream, or pending request.
  return (
    <SideChatContent
      key={`${props.userId ?? 'signed-out'}:${props.sourceJob.threadId ?? props.sourceJob.id}`}
      {...props}
    />
  );
}

function SideChatContent({ sourceJob, sourceJobs, userId, config, active }: SideChatProps) {
  const { session, status, error, retry, advance } = useSideChatSession(
    sourceJob,
    userId,
    active,
    sourceJobs,
  );
  const { job, threadJobs, loading, error: loadError, reload } = useAgentJob(session?.jobId ?? '');
  const draftKey = userId
    ? `${sideChatStorageKey(userId, sourceJob.threadId ?? sourceJob.id)}:draft`
    : '';
  const [draft, setDraft] = useState(() => {
    try {
      return draftKey ? (sessionStorage.getItem(draftKey) ?? '') : '';
    } catch {
      return '';
    }
  });
  const [model, setModel] = useState(sourceJob.model);
  const [effort, setEffort] = useState(sourceJob.reasoningEffort ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
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
  const ready = Boolean(current && !loading && !loadError);
  const running = current?.state === 'running' || current?.state === 'queued';
  const unavailable = Boolean(
    current?.archivedAt || current?.nativeState === 'lost' || current?.contextStatus === 'lost',
  );

  useEffect(() => {
    if (!current || modelInitialized.current) return;
    modelInitialized.current = true;
    setModel(current.model);
    setEffort(current.reasoningEffort ?? '');
  }, [current]);

  const efforts = useMemo(
    () => offeredEfforts(config, current?.runtime ?? sourceJob.runtime, model),
    [config, current?.runtime, model, sourceJob.runtime],
  );
  useEffect(() => {
    if (config && modelInitialized.current && !efforts.includes(effort)) {
      setEffort(highestEffort(efforts));
    }
  }, [config, effort, efforts]);
  const models = config?.models?.length
    ? [...new Set([current?.model ?? sourceJob.model, ...config.models])]
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
    const latest = threadJobs.reduce(
      (found, row) => ((row.turn_no ?? 0) > (found?.turn_no ?? 0) ? row : found),
      threadJobs[0],
    );
    if (latest && latest.id !== session.jobId) {
      try {
        advance(latest);
      } catch (cause) {
        setActionError(cause instanceof Error ? cause.message : 'Could not load side chat.');
      }
    }
  }, [advance, ready, session, threadJobs]);

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
    const history = describeHistoryTurns(job).filter((turn) => own.has(turn.jobId));
    if (!own.has(job.id)) return history;
    const turn = describeCurrentTurn(job, { attemptNo: job.currentAttemptNo });
    const messages = [...turn.messages];
    if (turn.promptCard !== null) {
      messages.unshift({
        id: -1,
        role: 'user',
        content: turn.promptCard,
        jobId: job.id,
        createdAt: null,
      });
    }
    return [
      ...history,
      {
        ...turn,
        messages,
        fallbackResponses: turn.events.some((event) => event.kind === 'message')
          ? []
          : turn.messages.filter((message) => message.role === 'assistant'),
      },
    ];
  }, [belongsToSide, job, session?.threadId, threadJobs]);
  const turnKeys = useMemo(() => turns.map((turn) => turn.key), [turns]);
  const scroll = useConversationScroll({
    conversationKey: session?.threadId ?? 'side-chat-preparing',
    turnKeys,
    active: active && !toolsOpen,
  });

  useEffect(() => {
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
  }, [active, current, ready, toolsOpen, userId]);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = draft.trim();
    if (!ready || !current || !prompt || unavailable || running || submission.current) return;
    submission.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      const next = await followUpAgentJob(current.id, {
        prompt,
        ...(model !== current.model ? { model } : {}),
        ...(effort && effort !== current.reasoningEffort ? { reasoning_effort: effort } : {}),
      });
      if (!mounted.current) return;
      advance(next);
      setDraft('');
      scroll.followLatest();
    } catch (cause) {
      if (mounted.current) {
        setActionError(cause instanceof Error ? cause.message : 'Could not send this message.');
      }
    } finally {
      submission.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  async function stop() {
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
        setActionError(cause instanceof Error ? cause.message : 'Could not stop side chat.');
      }
    }
  }

  const failure = error ?? loadError ?? actionError;
  return (
    <section aria-label="Side chat" className="flex h-full min-h-0 flex-col">
      {toolsOpen && current ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <button
            type="button"
            onClick={() => setToolsOpen(false)}
            className="mb-4 text-sm text-gray-600 hover:underline dark:text-gray-400"
          >
            Back to side chat
          </button>
          <McpSessionPanel jobId={current.id} threadId={current.threadId!} active={active} />
        </div>
      ) : (
        <div
          ref={scroll.containerRef}
          role="region"
          aria-label="Side chat messages"
          tabIndex={0}
          className="flex min-h-0 flex-1 flex-col overflow-y-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40"
        >
          <div className="flex-1 px-5 pt-3">
            {turns.map((turn) => (
              <HistoryTurn key={turn.key} turn={turn} busy={false} />
            ))}
            {turns.length && current?.liveNote ? (
              <p role="status" className="py-2 text-xs text-gray-500 dark:text-gray-400">
                {current.liveNote}
              </p>
            ) : null}
            {turns.length && current?.stateNote ? (
              <p className="py-2 text-xs text-gray-500 dark:text-gray-400">{current.stateNote}</p>
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
                onReview={() => setToolsOpen(true)}
              />
            ) : null}
            <form
              onSubmit={(event) => void send(event)}
              className="rounded-xl border border-gray-200 bg-white focus-within:border-crimson/30 dark:border-white/10 dark:bg-gray-900"
            >
              <textarea
                aria-label="Message side chat"
                placeholder="Message side chat"
                rows={2}
                value={draft}
                disabled={unavailable || !userId}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (!isComposerSubmit(event)) return;
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }}
                className="w-full resize-none rounded-t-xl border-0 bg-transparent px-4 pt-3 text-sm leading-relaxed text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 dark:text-gray-50 dark:placeholder:text-gray-500"
              />
              <div className="flex flex-wrap items-center gap-1 px-3 pb-2">
                {models.length ? (
                  <>
                    <Picker
                      label="Side chat model"
                      value={model}
                      options={models}
                      onChange={setModel}
                    />
                    <Picker
                      label="Side chat reasoning"
                      value={effort}
                      options={efforts}
                      onChange={setEffort}
                    />
                  </>
                ) : (
                  <span className="min-w-0 flex-1 truncate text-xs text-gray-500 dark:text-gray-400">
                    {model}
                  </span>
                )}
                {running ? (
                  <button
                    type="button"
                    aria-label="Stop side chat"
                    disabled={stopping}
                    onClick={() => void stop()}
                    className="ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gray-900 text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
                  >
                    <span className="h-2.5 w-2.5 rounded-sm bg-current" />
                  </button>
                ) : (
                  <button
                    type="submit"
                    aria-label="Send side chat message"
                    disabled={!ready || unavailable || !draft.trim() || submitting}
                    className="ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-crimson text-white hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {submitting || (active && status === 'preparing') ? (
                      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                    ) : (
                      <svg
                        aria-hidden="true"
                        className="h-4 w-4"
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                        strokeWidth={2}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M12 19V5m0 0-6 6m6-6 6 6"
                        />
                      </svg>
                    )}
                  </button>
                )}
              </div>
            </form>
            {status === 'waiting' ? (
              <p role="status" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                Waiting for the main conversation to finish…
              </p>
            ) : null}
            {unavailable ? (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                {current?.archivedAt
                  ? 'This side chat is archived.'
                  : 'Unable to reconnect this side chat.'}
              </p>
            ) : null}
            {failure ? (
              <div role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
                {ownerFailureMessage(failure)}
                {error || loadError ? (
                  <button type="button" onClick={error ? retry : reload} className="ml-2 underline">
                    Retry
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
