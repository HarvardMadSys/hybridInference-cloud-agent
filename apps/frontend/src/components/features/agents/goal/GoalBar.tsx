'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal } from '@/lib/api/agents';
import type { AgentGoal } from '../types';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { GoalIcon } from '../ComposerPlusMenu';
import { writeGoal } from './goalApi';
import { useGoalWrite } from './useGoalWrite';
import { GoalActionsMenu } from './GoalActionsMenu';
import { DEFAULT_GOAL_LIMITS, type GoalLimits } from '@/lib/goalLimits';

/** What the band needs that the goal row does not carry. */
export interface GoalBandState {
  /** A turn of this conversation is queued or running right now. */
  live: boolean;
  /** The turn in flight is a repair round rather than work. */
  repairing: boolean;
}

/**
 * The objective leads the composer; status and round accounting sit beneath it.
 * Pause/resume stays visible, while editing and clearing share the action menu.
 * The editor keeps its draft across reloads and only closes once a write lands.
 */
export function GoalBar({
  threadId,
  goal,
  limits = DEFAULT_GOAL_LIMITS,
  state,
  onChanged,
  onCleared,
  attached = false,
}: {
  threadId: string;
  goal: AgentGoal;
  limits?: GoalLimits;
  state: GoalBandState;
  /** Re-read the authoritative thread goal after a write or revision conflict. */
  onChanged: () => void;
  /** Return focus to the message box when clearing removes this component. */
  onCleared?: () => void;
  /** Share the message box's outer surface instead of drawing a separate card. */
  attached?: boolean;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);
  const { busy, error, run, clearError } = useGoalWrite(onChanged);

  useEffect(() => {
    if (wasEditing.current && !editing) trigger.current?.focus();
    wasEditing.current = editing;
  }, [editing]);

  const rounds = roundsText(t, goal);
  const paused = goal.state === 'paused';
  const complete = goal.state === 'complete';
  const needsInput = paused && goal.pauseReason === 'needs_input';
  const waiting = goal.state === 'active' && !state.live;
  const spent = goal.maxRounds != null && goal.roundsUsed >= goal.maxRounds;
  const activeStatus = state.repairing
    ? 'goal.activeRepairing'
    : waiting
      ? 'goal.activeWaiting'
      : 'goal.active';
  const status = complete
    ? t('goal.complete')
    : paused
      ? t(needsInput ? 'goal.needsInput' : 'goal.paused')
      : t(activeStatus);
  const reason = paused
    ? `${t(goal.pauseReason === 'fork' ? 'goal.pausedFork' : pauseKey(goal.pauseReason))}${goal.pauseDetail ? ` · ${goal.pauseDetail}` : ''}`
    : complete && goal.completionBasis === 'model_reported'
      ? t('goal.completeReported')
      : null;
  const resumeHint = spent ? t('goal.limitResumeLiftsHint') : t('goal.resumeHint');

  return (
    <section
      aria-label={t('goal.region')}
      className={`bg-gray-50/80 px-3 py-2 text-gray-600 dark:bg-gray-950/40 dark:text-gray-300 ${
        attached
          ? 'rounded-t-xl border-b border-gray-200/80 dark:border-white/10'
          : 'mb-2 rounded-xl border border-gray-200 dark:border-white/10'
      }`}
    >
      <div className="flex items-start gap-2.5">
        <GoalIcon className="mt-1 h-4 w-4 shrink-0 text-crimson dark:text-crimson-light" />
        <div className="min-w-0 flex-1">
          {!editing ? (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              aria-expanded={expanded}
              title={goal.objective}
              className={`block w-full min-w-0 rounded py-0.5 text-left text-sm font-medium text-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-crimson dark:text-gray-100 ${
                expanded ? 'whitespace-pre-wrap break-words' : 'truncate'
              }`}
            >
              {goal.objective}
            </button>
          ) : null}
          <div role="status" aria-label={t('attach.goal')}>
            <div
              className={`flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs ${editing ? 'pt-1' : 'mt-1.5'}`}
            >
              <span
                className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 ${
                  paused
                    ? 'bg-amber-50 text-amber-800 dark:bg-amber-400/10 dark:text-amber-200'
                    : waiting
                      ? 'bg-gray-100 text-gray-600 dark:bg-white/5 dark:text-gray-300'
                      : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-200'
                }`}
              >
                <StatusIcon
                  kind={complete ? 'complete' : paused ? 'paused' : waiting ? 'waiting' : 'active'}
                />
                {status}
              </span>
              <span className="text-gray-500 dark:text-gray-400">{rounds}</span>
            </div>
            {reason ? (
              <p className="mt-2 break-words text-xs leading-relaxed text-gray-600 dark:text-gray-400">
                {reason}
              </p>
            ) : null}
            {paused && spent ? (
              <p className="mt-2 text-xs leading-relaxed text-gray-600 dark:text-gray-400">
                {t('goal.limitResumeLifts', { max: goal.maxRounds ?? 0 })}
              </p>
            ) : null}
            {paused && state.live ? (
              <p className="mt-2 text-xs leading-relaxed text-gray-600 dark:text-gray-400">
                {t('goal.pausedRunning')}
              </p>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          {!complete && !editing ? (
            <GoalAction
              disabled={busy}
              tone={paused ? 'primary' : 'plain'}
              label={t(paused ? 'goal.resume' : 'goal.pause')}
              hint={paused ? resumeHint : t('goal.pauseHint')}
              onClick={() =>
                void run(() => (paused ? resumeAgentGoal(threadId) : pauseAgentGoal(threadId)))
              }
            >
              {paused ? <ResumeIcon /> : <PauseIcon />}
            </GoalAction>
          ) : null}
          <GoalActionsMenu
            triggerRef={trigger}
            canEdit={!complete}
            disabled={busy || editing}
            onEdit={() => {
              clearError();
              setEditing(true);
            }}
            onClear={() => {
              void run(() => clearAgentGoal(threadId)).then((landed) => {
                if (landed) onCleared?.();
              });
            }}
          />
        </div>
      </div>
      {editing ? (
        <div className="mt-3 sm:ml-[26px]">
          <GoalEditor
            initial={goal.objective}
            limits={limits}
            busy={busy}
            error={error}
            onCancel={() => {
              clearError();
              setEditing(false);
            }}
            onSave={(objective) => {
              // A reload may update the revision, but the mounted editor owns
              // its draft until the server accepts it or the owner cancels.
              void run(() => writeGoal(threadId, objective, goal.revision)).then((landed) => {
                if (landed) setEditing(false);
              });
            }}
          />
        </div>
      ) : null}
      {error && !editing ? (
        <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/**
 * Editing the objective, on the row it is already on.
 *
 * Mounted only while editing, which is what makes the draft survive everything
 * that can happen during a write: a compare-and-set refusal re-reads the
 * conversation and moves `goal.objective` under this component, and because the
 * draft is this component's own state, seeded once, the other tab's words never
 * land in the box the owner is typing in. Unmounting — Cancel, Escape, or a
 * write that landed — is how a draft is abandoned.
 */
function GoalEditor({
  initial,
  limits,
  busy,
  error,
  onSave,
  onCancel,
}: {
  initial: string;
  limits: GoalLimits;
  busy: boolean;
  error: string | null;
  onSave: (objective: string) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const fieldId = useId();
  const field = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState(initial);

  useEffect(() => {
    const control = field.current;
    if (!control) return;
    control.focus();
    // Keep the caret at the end so the owner can append to the current goal.
    control.setSelectionRange(control.value.length, control.value.length);
  }, []);

  const trimmed = draft.trim();
  const tooLong = draft.length > limits.max_objective_chars;
  const canSave = trimmed.length > 0 && !tooLong && !busy;

  return (
    <div
      className="w-full"
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          if (!busy) onCancel();
        }
      }}
    >
      <label htmlFor={fieldId} className="mb-1.5 block text-xs text-gray-500 dark:text-gray-400">
        {t('goal.objective')}
      </label>
      <textarea
        id={fieldId}
        ref={field}
        rows={2}
        value={draft}
        readOnly={busy}
        maxLength={limits.max_objective_chars}
        aria-invalid={tooLong || undefined}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // An IME's Escape cancels the candidate it is showing, not the edit.
          // The composers' own guard reads the same two fields for Enter.
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          // The objective is prose and may run to several lines, so Enter is a
          // newline here. Cmd/Ctrl+Enter is the chord that saves.
          if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            if (canSave) onSave(trimmed);
          }
        }}
        className="block w-full resize-y rounded-lg border border-gray-200 bg-white px-3 py-2.5 text-base leading-relaxed text-gray-900 focus:border-crimson/40 focus:outline-none focus:ring-2 focus:ring-crimson/10 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100 sm:text-sm"
      />
      <div className="mt-2 flex flex-wrap items-center justify-end gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="inline-flex min-h-8 items-center rounded-lg px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-200/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-crimson disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-300 dark:hover:bg-white/10 [@media(pointer:coarse)]:min-h-11"
        >
          {t('goal.editCancel')}
        </button>
        <button
          type="button"
          disabled={!canSave}
          onClick={() => onSave(trimmed)}
          className="inline-flex min-h-8 items-center rounded-lg bg-crimson px-3 py-1.5 text-xs font-medium text-white hover:bg-crimson-dark focus-visible:outline focus-visible:outline-2 focus-visible:outline-crimson disabled:cursor-not-allowed disabled:opacity-40 [@media(pointer:coarse)]:min-h-11"
        >
          {busy ? t('goal.editSaving') : t('goal.editSave')}
        </button>
        {tooLong ? (
          <span role="alert" className="w-full text-xs text-red-600 dark:text-red-400">
            {t('goal.objectiveTooLong', { max: limits.max_objective_chars })}
          </span>
        ) : null}
        {error ? (
          <span role="alert" className="w-full text-xs text-red-600 dark:text-red-400">
            {error}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * How many rounds this goal has run, as a sentence.
 *
 * A count, and a total only when the owner stated one. The band used to read
 * "3/10" for every goal, which reported a limit nobody had set and made the
 * absence of a limit unrepresentable.
 */
function roundsText(
  t: ReturnType<typeof useT>,
  goal: Pick<AgentGoal, 'roundsUsed' | 'maxRounds'>,
): string {
  if (goal.maxRounds === null || goal.maxRounds === undefined) {
    return t('goal.roundsRun', { used: goal.roundsUsed });
  }
  return t('goal.roundsBounded', { used: goal.roundsUsed, max: goal.maxRounds });
}

/** One icon control, named for assistive technology and explained for a pointer. */
function GoalAction({
  children,
  onClick,
  disabled,
  label,
  hint,
  tone = 'plain',
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  label: string;
  hint: string;
  tone?: 'plain' | 'primary';
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={hint}
      className={`inline-flex min-h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg px-2 py-1.5 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-crimson disabled:cursor-not-allowed disabled:opacity-40 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11 ${
        tone === 'primary'
          ? 'bg-crimson text-white hover:bg-crimson-dark'
          : 'text-gray-500 hover:bg-gray-200/60 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100'
      }`}
    >
      {children}
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}

const ICON = 'h-3.5 w-3.5';

function PauseIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      aria-hidden="true"
      className={ICON}
    >
      <path strokeLinecap="round" d="M9.5 5v14M14.5 5v14" />
    </svg>
  );
}

function ResumeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      aria-hidden="true"
      className={ICON}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 5.2 19 12 7.5 18.8V5.2Z" />
    </svg>
  );
}

function StatusIcon({ kind }: { kind: 'active' | 'waiting' | 'paused' | 'complete' }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      className="h-3 w-3 shrink-0"
    >
      {kind === 'complete' ? (
        <path strokeLinecap="round" strokeLinejoin="round" d="m5 12 4 4L19 6" />
      ) : kind === 'paused' ? (
        <path strokeLinecap="round" d="M9 5v14M15 5v14" />
      ) : kind === 'waiting' ? (
        <>
          <circle cx="12" cy="12" r="8" />
          <path strokeLinecap="round" d="M12 7v5l3 2" />
        </>
      ) : (
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 12h4l3-7 4 14 3-7h4" />
      )}
    </svg>
  );
}

/** The dictionary key for a pause reason the server recorded. */
const PAUSE_KEYS: Record<string, TranslationKey> = {
  user: 'goal.pauseUser',
  fork: 'goal.pauseFork',
  needs_input: 'goal.pauseNeedsInput',
  round_limit: 'goal.pauseRoundLimit',
  execution_failed: 'goal.pauseExecutionFailed',
  cancelled: 'goal.pauseCancelled',
  report_invalid: 'goal.pauseReportInvalid',
  report_unavailable: 'goal.pauseReportUnavailable',
  runtime_unsupported: 'goal.pauseRuntimeUnsupported',
  context_lost: 'goal.pauseContextLost',
  access_denied: 'goal.pauseAccessDenied',
  quota_unavailable: 'goal.pauseQuotaUnavailable',
  scheduling_failed: 'goal.pauseSchedulingFailed',
  archived: 'goal.pauseArchived',
};

/** An unrecognised reason is still a reason: say it stopped, not what it was. */
function pauseKey(reason: string | null | undefined): TranslationKey {
  return (reason && PAUSE_KEYS[reason]) || 'goal.pauseUnknown';
}
