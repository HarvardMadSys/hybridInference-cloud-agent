'use client';

import { useT } from '@/components/providers';
import type { SelectedSkillApi } from '@/lib/api/skills';
import { filesFromDataTransfer } from '@/lib/dataTransferFiles';
import { Suspense, useState, type FormEvent, type RefObject } from 'react';
import { ComposerCommandMenu } from '../ComposerCommandMenu';
import { ComposerDialogLoading } from '../ComposerDialogLoading';
import { ComposerPlusMenu } from '../ComposerPlusMenu';
import { ComposerMessageActions, ComposerTextarea } from '../ConversationComposer';
import { LiveAgentStatus } from '../LiveAgentStatus';
import { NativeQuestionPrompt } from '../NativeQuestionPrompt';
import { PendingMessageQueue } from '../PendingMessageQueue';
import { Picker } from '../Picker';
import { PromptRewrite } from '../PromptRewrite';
import { ThreadStatsBar } from '../ThreadStatsBar';
import { ownerFailureMessage } from '../adapt';
import { AttachmentStrip } from '../attachments/AttachmentStrip';
import type { useAttachments } from '../attachments/useAttachments';
import { isSteerChord } from '../composerKeys';
import { hasLostContext } from '../conversationJob';
import { GoalBar } from '../goal/GoalBar';
import { hasLiveWork } from '../goal/liveWork';
import type { useGoalCommand } from '../goal/useGoalCommand';
import { McpSessionPanel } from '../mcp/McpSessionPanel';
import type { useThreadComposerMcp } from '../mcp/useThreadComposerMcp';
import type { AgentJob } from '../types';
import type { useComposerDraft } from '../useComposerDraft';
import type { usePendingMessages } from '../usePendingMessages';
import { SkillSelectionDialog } from './LazyPanels';
import type { useJobComposerSettings } from './useJobComposerSettings';
import type { useJobRunActions } from './useJobRunActions';

/** Composer controls and dialogs; request handling remains with the page. */
export function JobComposer({
  job,
  settings,
  runActions,
  draft,
  attachments,
  mcp,
  queue,
  goalCommand,
  composerRef,
  followUpBox,
  questionTurn,
  goalHint,
  setGoalHint,
  selectedSkills,
  setChosenSkills,
  mcpBlockedReason,
  submitting,
  rewriting,
  readingFiles,
  setReadingFiles,
  setRewriting,
  actionError,
  setActionError,
  submitFollowUp,
  steerNow,
  insertGoalCommand,
  onReload,
  hidden = false,
}: {
  job: AgentJob;
  settings: ReturnType<typeof useJobComposerSettings>;
  runActions: ReturnType<typeof useJobRunActions>;
  draft: ReturnType<typeof useComposerDraft>;
  attachments: ReturnType<typeof useAttachments>;
  mcp: ReturnType<typeof useThreadComposerMcp>;
  queue: ReturnType<typeof usePendingMessages>;
  goalCommand: ReturnType<typeof useGoalCommand>;
  composerRef: (element: HTMLElement | null) => void;
  followUpBox: RefObject<HTMLTextAreaElement>;
  questionTurn: { id: string; attemptId?: number | null; active: boolean };
  goalHint: string | null;
  setGoalHint: (hint: string | null) => void;
  selectedSkills: SelectedSkillApi[];
  setChosenSkills: (skills: SelectedSkillApi[]) => void;
  mcpBlockedReason?: string;
  submitting: boolean;
  rewriting: boolean;
  readingFiles: boolean;
  setReadingFiles: (reading: boolean) => void;
  setRewriting: (rewriting: boolean) => void;
  actionError: string | null;
  setActionError: (error: string | null) => void;
  submitFollowUp: (event: FormEvent<HTMLFormElement>) => void;
  steerNow: () => void;
  insertGoalCommand: () => void;
  onReload?: () => void;
  /** Kept mounted, with its draft, while another view replaces the Chat. */
  hidden?: boolean;
}) {
  const t = useT();
  const threadId = job.threadId ?? job.id;
  const isArchived = Boolean(job.archivedAt);
  const isActive = job.state === 'running' || job.state === 'queued';
  const contextLost = hasLostContext(job);
  const {
    config,
    inherited,
    model,
    setChosenModel,
    effort,
    setChosenEffort,
    modelOptions,
    efforts,
  } = settings;
  const { restoring, unarchive } = runActions;
  const { value: followUp, setValue: setFollowUp } = draft;
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [dropping, setDropping] = useState(false);
  return (
    <>
      {/* Keep the sticky controls opaque while the transcript scrolls behind them. */}
      <div
        ref={composerRef}
        hidden={hidden}
        className="sticky bottom-0 z-10 -mx-1 mt-3 bg-gray-50 px-1 pb-1 pt-2 dark:bg-gray-950"
      >
        <LiveAgentStatus job={job} />
        {/* `/goal` on its own is not an objective. It is consumed
                    rather than sent to the model, and this is where the owner
                    is told what is missing — beside the box the token is still
                    sitting in. */}
        {goalHint ? (
          <p className="mb-2 text-[11px] text-gray-500 dark:text-gray-400" role="status">
            {goalHint}
          </p>
        ) : null}
        <NativeQuestionPrompt
          threadId={threadId}
          jobId={questionTurn.id}
          attemptId={questionTurn.attemptId}
          active={questionTurn.active && !isArchived}
        />
        <McpSessionPanel
          jobId={job.id}
          threadId={job.threadId ?? job.id}
          active={isActive}
          approvalsOnly
        />
        {/* Its own card above the composer rather than inside it: the
                    queue is what the conversation will send next, not part of
                    the message being written. */}
        <PendingMessageQueue queue={queue} disabled={isArchived || contextLost} />
        <div
          className={
            isArchived
              ? undefined
              : `ui-composer ${dropping ? 'border-accent/40 ring-4 ring-accent/[0.08]' : ''}`
          }
        >
          {job.goal ? (
            <GoalBar
              attached={!isArchived}
              onCleared={() => followUpBox.current?.focus()}
              threadId={threadId}
              goal={job.goal}
              limits={config?.goal_limits}
              state={{
                live: hasLiveWork(job),
                repairing: job.goalTurn?.kind === 'repair' && isActive,
              }}
              onChanged={() => {
                // The goal belongs to the conversation, so the page's own
                // reload is what re-reads it. Reimplementing
                // fetch-and-adapt here would be a second reader of the
                // same endpoint, and the two would drift.
                onReload?.();
              }}
            />
          ) : null}
          {isArchived ? (
            <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-white/10 dark:bg-gray-900">
              <div className="min-w-0 flex-1 basis-64">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {t('sessionArchive.title')}
                </p>
                <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                  {t(
                    contextLost
                      ? 'sessionArchive.contextLost'
                      : isActive
                        ? 'sessionArchive.running'
                        : 'sessionArchive.body',
                  )}
                </p>
              </div>
              <button
                type="button"
                disabled={restoring}
                onClick={() => void unarchive()}
                className="rounded-lg bg-gray-900 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
              >
                {t(
                  restoring
                    ? 'sessionArchive.restoring'
                    : contextLost
                      ? 'sessionArchive.restore'
                      : 'sessionArchive.continue',
                )}
              </button>
            </div>
          ) : (
            <form
              onSubmit={(event) => void submitFollowUp(event)}
              onDragOver={(event) => {
                if (contextLost || submitting || !event.dataTransfer.types.includes('Files'))
                  return;
                event.preventDefault();
                setDropping(true);
              }}
              onDragLeave={(event) => {
                if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
                setDropping(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setDropping(false);
                if (contextLost || submitting) return;
                setReadingFiles(true);
                void filesFromDataTransfer(event.dataTransfer)
                  .then(attachments.add)
                  .catch((cause: unknown) => {
                    setActionError(
                      cause instanceof Error ? cause.message : 'Could not read dropped files',
                    );
                  })
                  .finally(() => setReadingFiles(false));
              }}
            >
              <ComposerCommandMenu
                id={goalCommand.menuId}
                entries={goalCommand.entries}
                activeIndex={goalCommand.activeIndex}
                onHighlight={goalCommand.onHighlight}
                onChoose={goalCommand.onChoose}
              />
              <ComposerTextarea
                textareaRef={followUpBox}
                rows={2}
                value={followUp}
                onPaste={(event) => {
                  if (contextLost || submitting) return;
                  attachments.onPaste(event);
                }}
                onChange={(event) => {
                  setFollowUp(event.target.value);
                  if (goalHint) setGoalHint(null);
                }}
                // The menu owns the keys while it is showing: Enter
                // confirms the highlighted command rather than sending
                // half of one to the model.
                onKeyDown={(event) => {
                  goalCommand.onKeyDown(event);
                  if (event.defaultPrevented) return;
                  if (queue.canSendNow && isSteerChord(event)) {
                    event.preventDefault();
                    void steerNow();
                  }
                }}
                // The list is this box's own popup, and the highlighted
                // row is named to it — the caret never leaves here.
                // `aria-expanded` is deliberately not used: the textbox
                // role does not support it, and `aria-controls` pointing
                // at a list that is only in the DOM while it is open says
                // the same thing without lying.
                aria-autocomplete="list"
                aria-controls={goalCommand.open ? goalCommand.menuId : undefined}
                aria-activedescendant={goalCommand.activeId}
                disabled={contextLost}
                placeholder={
                  contextLost
                    ? 'Unable to reconnect this chat'
                    : 'Message or run a task, / commands, @ files or sessions'
                }
                aria-label="Add a follow-up"
                className="block max-h-[30vh] min-h-12 w-full resize-y rounded-t-xl border-0 bg-transparent px-3 py-2 text-sm leading-6 text-foreground placeholder:text-muted focus:outline-none focus:ring-0"
              />
              <AttachmentStrip
                entries={attachments.entries}
                onRemove={attachments.remove}
                disabled={contextLost || submitting}
              />
              <div className="flex flex-wrap items-center gap-1 border-t border-border px-2 py-1">
                <ComposerPlusMenu
                  onFiles={attachments.add}
                  onOpenSkills={() => setSkillsOpen(true)}
                  skillSummary={String(selectedSkills.length)}
                  goalSummary={t('commands.goalHint')}
                  onOpenGoal={insertGoalCommand}
                  mcp={mcp}
                  onOpen={() => {
                    goalCommand.dismiss();
                    mcp.reload();
                  }}
                  mcpBlockedReason={mcpBlockedReason}
                  disabled={contextLost || submitting}
                />
                <span aria-hidden="true" className="mx-1 h-5 w-px bg-border" />
                {modelOptions.length > 0 ? (
                  <>
                    {/* The harness stays the thread's; the model is offered
                          again. A model change restarts this member's CLI and
                          native-resumes it in the same retained workspace; it
                          does not replace or clone the sandbox. */}
                    <Picker
                      compact
                      label="Model for this turn"
                      value={model}
                      options={modelOptions}
                      onChange={setChosenModel}
                    />
                    <Picker
                      compact
                      label="Reasoning for this turn"
                      value={effort}
                      options={efforts}
                      onChange={setChosenEffort}
                    />
                  </>
                ) : null}
                <PromptRewrite
                  key={threadId}
                  compact
                  inline
                  value={followUp}
                  model={model}
                  disabled={
                    contextLost || submitting || goalCommand.busy || /^\s*\//.test(followUp)
                  }
                  onActiveChange={setRewriting}
                  onApply={(prompt) => {
                    setFollowUp(prompt);
                    followUpBox.current?.focus();
                  }}
                />
                <ComposerMessageActions
                  label={t(isActive ? 'composer.queue' : 'composer.send')}
                  title={t(isActive ? 'composer.queueHint' : 'composer.sendHint')}
                  steerLabel={t('composer.steer')}
                  steerTitle={t('composer.steerHint')}
                  onSteer={
                    queue.canSendNow && attachments.entries.length === 0
                      ? () => void steerNow()
                      : undefined
                  }
                  disabled={
                    contextLost ||
                    !followUp.trim() ||
                    submitting ||
                    rewriting ||
                    attachments.uploading ||
                    readingFiles ||
                    mcp.saving ||
                    goalCommand.busy
                  }
                  busy={submitting || goalCommand.busy}
                />
              </div>
              <p className="px-3 pb-2 text-[11px] leading-4 text-muted">
                {contextLost
                  ? `Start a new chat · ${job.runtime}`
                  : job.state === 'cancelled'
                    ? `Restores any saved changes · ${job.runtime}`
                    : `Inherits ${job.runtime}`}
                {modelOptions.length === 0 ? ` · ${inherited.model}` : ''}
                {isActive ? ` · ${t('queue.afterThisTurn')}` : ''}
                {queue.canSendNow ? ` · ${t('queue.steerHint')}` : ''}
              </p>
            </form>
          )}
        </div>
        {skillsOpen ? (
          <Suspense
            fallback={
              <ComposerDialogLoading
                title={t('skills.title')}
                onClose={() => setSkillsOpen(false)}
              />
            }
          >
            <SkillSelectionDialog
              limits={config?.skill_limits}
              selected={selectedSkills}
              onClose={() => setSkillsOpen(false)}
              onApply={(skills) => {
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
        <ThreadStatsBar job={job} />
        {/* A goal write's refusal is drawn inside the editor that made
                    it, beside the words it refused; this line is the composer's
                    own failures, including a command's. */}
        {actionError ? (
          <p className="mt-2 text-center text-xs text-red-600 dark:text-red-400" role="alert">
            {ownerFailureMessage(actionError)}
          </p>
        ) : null}
      </div>
    </>
  );
}
