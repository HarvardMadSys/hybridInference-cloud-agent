'use client';

import { useT } from '@/components/providers';
import { memo, useCallback, useRef, type ComponentProps, type ReactNode } from 'react';
import { AttemptTurnFooter } from '../AttemptTurnFooter';
import {
  CopyIcon,
  copyMessage,
  EventList,
  EventRow,
  HistoryTurn,
  MessageActionButton,
  MessageActions,
  MessageContent,
  RewindIcon,
  ThreadTurn,
} from '../ChatMessages';
import { ownerRunNote } from '../conversationJob';
import { messageBlockKey, type ConversationTurnDescription } from '../conversationTurnNavigation';
import type { RecordedFileSelection, RecordedTurnOutputs } from '../recordedTurnOutputs';
import type { TranscriptPresentation } from '../transcriptPresentation';
import { TurnDeliverables } from '../TurnDeliverables';
import type { AgentEvent, AgentJob } from '../types';
import { OutcomeCard, RunNotices } from './RunOutcome';
import type { JobDetailPanels } from './useJobDetailPanels';

/** A stored turn of the transcript, with the results it recorded. */
function StoredTurnView({
  outputs,
  onOpenFile,
  ...props
}: Omit<ComponentProps<typeof HistoryTurn>, 'results'> & {
  outputs?: RecordedTurnOutputs | null;
  onOpenFile: (selection: RecordedFileSelection) => void;
}) {
  return (
    <HistoryTurn
      {...props}
      results={
        <TurnDeliverables
          job={{ id: props.turn.jobId, state: props.turn.state }}
          outputs={outputs}
          onOpenFile={onOpenFile}
        />
      }
    />
  );
}

// A stored turn does not change while the current one streams or the owner
// types, so it is drawn again only when its own description or actions do.
const StoredTurn = memo(StoredTurnView);

/** Renders conversation turns; navigation and mutations remain with the page. */
export function JobTranscript({
  panels,
  job,
  historyTurns,
  currentTurn,
  presentation,
  hasStoredEvents,
  rewindAnchors,
  currentRewindAnchor,
  canForkWorkspace,
  canForkCurrent,
  forking,
  forkFrom,
  attemptNo,
  liveAttempt,
  onSelectAttempt,
  selectedAttemptIsCurrent,
  usageFooter,
  firstRun,
  recordedOutputs,
  onReview,
  onWatchPr,
  onPublish,
}: {
  panels: JobDetailPanels;
  job: AgentJob;
  historyTurns: ConversationTurnDescription[];
  currentTurn: ConversationTurnDescription;
  presentation: TranscriptPresentation;
  hasStoredEvents: boolean;
  rewindAnchors: (string | undefined)[];
  currentRewindAnchor?: string;
  canForkWorkspace: boolean;
  canForkCurrent: boolean;
  forking: boolean;
  forkFrom: (anchorJobId: string, draft?: string) => void;
  attemptNo: number;
  liveAttempt: number;
  onSelectAttempt: (attemptNo: number) => void;
  selectedAttemptIsCurrent: boolean;
  usageFooter: ReactNode;
  firstRun: boolean;
  recordedOutputs: Map<string, RecordedTurnOutputs | null>;
  onReview?: () => void;
  onWatchPr?: () => void;
  onPublish?: () => void;
}) {
  const t = useT();
  const { inspectTranscriptEvent, openRecordedFile, openRun, openWorkspace } = panels;
  // Stored turns are memoized, so their actions keep one identity and reach
  // the latest state through this ref instead of changing with every render.
  const storedTurnActions = useRef({ forkFrom, inspectTranscriptEvent, openRecordedFile });
  storedTurnActions.current = { forkFrom, inspectTranscriptEvent, openRecordedFile };
  const forkStoredTurn = useCallback(
    (anchorJobId: string) => void storedTurnActions.current.forkFrom(anchorJobId),
    [],
  );
  const rewindStoredTurn = useCallback(
    (anchorJobId: string, prompt: string) =>
      void storedTurnActions.current.forkFrom(anchorJobId, prompt),
    [],
  );
  const inspectStoredEvent = useCallback(
    (event: AgentEvent, eventJobId: string) =>
      storedTurnActions.current.inspectTranscriptEvent(event, eventJobId),
    [],
  );
  const openStoredFile = useCallback(
    (selection: RecordedFileSelection) => storedTurnActions.current.openRecordedFile(selection),
    [],
  );
  const threadMessages = currentTurn.messages;
  const visibleEvents = currentTurn.events;
  const selectedAttempt = job.attempts.find((attempt) => attempt.no === attemptNo);
  const runNote = ownerRunNote(job);
  const isSuccessful = job.state === 'done' || job.state === 'needs_review';
  const currentReplyId = [...threadMessages]
    .reverse()
    .find((message) => message.role === 'assistant' && message.jobId === job.id)?.id;
  const lastThreadAssistantMessageId = [...threadMessages]
    .reverse()
    .find((message) => message.role === 'assistant')?.id;
  const lastVisibleMessageIndex = visibleEvents.reduce(
    (last, event, index) => (event.kind === 'message' ? index : last),
    -1,
  );
  return (
    <div className="flex-1">
      {historyTurns.map((turn, index) => {
        const rewindAnchorJobId = rewindAnchors[index];
        return (
          <StoredTurn
            key={turn.key}
            turn={turn}
            presentation={presentation}
            onInspectEvent={hasStoredEvents ? inspectStoredEvent : undefined}
            outputs={recordedOutputs.get(turn.jobId)}
            onOpenFile={openStoredFile}
            showActivityDetails={false}
            rewindAnchorJobId={rewindAnchorJobId}
            busy={forking}
            onFork={turn.forkable && canForkWorkspace ? forkStoredTurn : undefined}
            onRewind={rewindAnchorJobId && canForkWorkspace ? rewindStoredTurn : undefined}
            onOpenRun={openRun}
          />
        );
      })}
      <section aria-label={`Turn ${currentTurn.displayNo}`} data-turn-key={currentTurn.key}>
        {threadMessages.map((message) => {
          // Only a prompt starts a turn a rewind could replace.
          const rewindAnchor =
            message.role === 'user' && !message.midTurn && currentRewindAnchor !== message.jobId
              ? currentRewindAnchor
              : undefined;
          return (
            <ThreadTurn
              key={message.id}
              message={message}
              goalTurn={currentTurn.goalTurn}
              presentation={presentation}
              blockKey={messageBlockKey(message.jobId, message.id)}
              footer={
                lastVisibleMessageIndex < 0 && message.id === currentReplyId
                  ? usageFooter
                  : undefined
              }
              busy={forking}
              onFork={
                message.role === 'assistant' &&
                message.id === lastThreadAssistantMessageId &&
                isSuccessful &&
                canForkCurrent
                  ? () => void forkFrom(message.jobId)
                  : undefined
              }
              onRewind={
                rewindAnchor && canForkWorkspace
                  ? () => void forkFrom(rewindAnchor, message.content)
                  : undefined
              }
            />
          );
        })}

        {job.historyIncludesPrompt ? null : (
          <div
            data-block-key={`${job.id}:prompt`}
            className="group my-2 flex items-start gap-2 rounded-xl border border-gray-200/90 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-2.5 shadow-subtle"
          >
            <div className="min-w-0 flex-1">
              <MessageContent text={job.prompt || job.title} presentation={presentation} />
            </div>
            {presentation !== 'compact' || (currentRewindAnchor && canForkWorkspace) ? (
              <MessageActions align="end" className="!mt-0 max-w-[45%] shrink-0">
                {presentation !== 'compact' ? (
                  <MessageActionButton
                    label="Copy message"
                    onClick={() => void copyMessage(job.prompt || job.title)}
                  >
                    <CopyIcon />
                    Copy
                  </MessageActionButton>
                ) : null}
                {currentRewindAnchor && canForkWorkspace ? (
                  <MessageActionButton
                    label="Edit & rewind"
                    onClick={() => void forkFrom(currentRewindAnchor, job.prompt || job.title)}
                    disabled={forking}
                  >
                    <RewindIcon />
                    Edit & rewind
                  </MessageActionButton>
                ) : null}
              </MessageActions>
            ) : null}
          </div>
        )}

        {runNote && job.state !== 'failed' ? (
          <p className="mb-2 text-xs text-gray-400 dark:text-gray-500">{runNote}</p>
        ) : null}

        {job.attempts.length > 1 ? (
          <details
            className="my-3 text-xs"
            onToggle={(event) => {
              if (!event.currentTarget.open) {
                onSelectAttempt(liveAttempt);
              }
            }}
          >
            <summary className="cursor-pointer text-gray-500 dark:text-gray-400">
              {t('conversation.executionHistory', { count: job.attempts.length })}
              {attemptNo !== liveAttempt
                ? ` · ${t('conversation.viewingExecution', { number: attemptNo })}`
                : ''}
            </summary>
            <p className="mt-2 text-gray-400 dark:text-gray-500">
              {t('conversation.executionHistoryHint')}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {job.attempts.map((attempt) => (
                <button
                  key={attempt.no}
                  type="button"
                  aria-pressed={attempt.no === attemptNo}
                  onClick={() => {
                    onSelectAttempt(attempt.no);
                  }}
                  className={`rounded-md px-2 py-1 ${
                    attempt.no === attemptNo
                      ? 'bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900'
                      : 'bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-white/15'
                  }`}
                >
                  {attempt.no} · {attempt.status}
                </button>
              ))}
            </div>
          </details>
        ) : null}

        {selectedAttempt?.status === 'superseded' && selectedAttempt.note ? (
          <div className="my-3 rounded-lg border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-[13px] text-amber-800 dark:text-amber-300">
            {selectedAttempt.note}
          </div>
        ) : null}

        <div aria-label="Agent activity" className="mt-2">
          <EventList
            events={visibleEvents}
            presentation={presentation}
            completed={job.state === 'done' || job.state === 'needs_review'}
            onInspectEvent={hasStoredEvents ? inspectTranscriptEvent : undefined}
            showActivityDetails={false}
            jobId={job.id}
            renderEvent={(event, index, blockKey) => (
              <EventRow
                event={event}
                presentation={presentation}
                jobId={job.id}
                onInspectEvent={hasStoredEvents ? inspectTranscriptEvent : undefined}
                blockKey={blockKey}
                busy={forking}
                firstRun={firstRun}
                footer={index === lastVisibleMessageIndex ? usageFooter : undefined}
                onFork={
                  event.kind === 'message' &&
                  index === lastVisibleMessageIndex &&
                  isSuccessful &&
                  canForkCurrent &&
                  selectedAttemptIsCurrent
                    ? () => void forkFrom(job.id)
                    : undefined
                }
              />
            )}
          />
          {job.state === 'queued' && visibleEvents.length === 0 ? (
            <div className="flex items-center gap-2 py-1 text-[13px] text-gray-500 dark:text-gray-400">
              <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400 dark:bg-gray-600" />
              Waiting to start
            </div>
          ) : null}
        </div>

        <AttemptTurnFooter job={job} attemptNo={attemptNo} />
        {selectedAttemptIsCurrent ? (
          <TurnDeliverables
            job={job}
            outputs={recordedOutputs.get(job.id)}
            onOpenFile={openRecordedFile}
          />
        ) : null}

        {/* The inspector's Run details open on the turn the reader
                        is looking at, which is this one. */}
        <div className="mt-2">
          <button
            type="button"
            onClick={() => openRun(job.id, job.turnNo ?? 1)}
            className="rounded-md px-1.5 py-0.5 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
          >
            View execution
          </button>
        </div>

        <OutcomeCard
          job={job}
          firstRun={firstRun}
          onOpenDiff={() => openWorkspace('git')}
          onReview={onReview}
          onWatchPr={onWatchPr}
          onPublish={onPublish}
        />
        {selectedAttemptIsCurrent ? <RunNotices job={job} /> : null}
        {usageFooter && lastVisibleMessageIndex < 0 && currentReplyId === undefined ? (
          <div className="mt-2">{usageFooter}</div>
        ) : null}
      </section>
    </div>
  );
}
