'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal } from '@/lib/api/agents';
import type { AgentGoal } from '../types';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { GoalIcon } from '../ComposerPlusMenu';
import { writeGoal } from './goalApi';
import { useGoalWrite } from './useGoalWrite';
import { DEFAULT_GOAL_LIMITS, type GoalLimits } from '@/lib/goalLimits';

/** What the band needs that the goal row does not carry. */
export interface GoalBandState {
  /** A turn of this conversation is queued or running right now. */
  live: boolean;
  /** The turn in flight is a repair round rather than work. */
  repairing: boolean;
}

/**
 * The goal's status, above the message box, and the only place it is managed.
 *
 * Three shapes, one per durable state. An owner looking at a stopped
 * conversation needs to know *which* stop it was, and that is data now: the
 * reason and the sentence behind it both come from the goal row, so a stop the
 * platform decided reads as specifically as one the owner asked for.
 *
 * The band is absent, not empty, when there is no goal. A conversation that is
 * simply a conversation must look like one.
 *
 * **Everything happens here.** Setting a goal is typing `/goal <objective>` in
 * the box below; reading, pausing, resuming and clearing it are the controls on
 * this line; and editing opens *in place*, on the row the objective is already
 * on, with Save and Cancel beside it. There is no dialog anywhere in the goal
 * feature, because there is nothing a dialog could hold that this band does not:
 * an objective, and the four things that can be done to it.
 *
 * Rounds are reported as a count of what has actually run. A total appears only
 * when the owner stated a bound themselves — which this product never asks for
 * and no client sends — so "3 rounds run" is the ordinary reading and "3 of at
 * most 10 rounds" is the exception.
 */
export function GoalBar({
  threadId,
  goal,
  limits = DEFAULT_GOAL_LIMITS,
  state,
  onChanged,
}: {
  threadId: string;
  goal: AgentGoal;
  limits?: GoalLimits;
  state: GoalBandState;
  /**
   * Called after a write lands. The goal belongs to the conversation, so the
   * caller re-reads it from the thread rather than from the write's answer.
   */
  onChanged: () => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const { busy, error, run } = useGoalWrite(onChanged);

  const rounds = roundsText(t, goal);
  // A bound is only ever present because the owner stated one, and there is no
  // surface that changes it. A goal sitting at that bound cannot be continued
  // while it still applies, so resuming is what lifts it — the server does
  // exactly that, and the band says so rather than offering a disabled button
  // and telling the owner to go and find a form that does not exist.
  const spent = goal.maxRounds !== null && goal.roundsUsed >= goal.maxRounds;

  let text: React.ReactNode;
  let actions: React.ReactNode;
  let hint: string | null = null;
  if (goal.state === 'active') {
    // Between rounds the platform is not idle, it is waiting for its own next
    // turn to be claimed — often because a person's message is queued ahead of
    // it, and sometimes because the round has not been queued yet. The label
    // says which, so "no round running" is never mistaken for "the goal
    // stopped".
    text = state.repairing
      ? t('goal.activeRepairing', { rounds })
      : state.live
        ? t('goal.active', { rounds })
        : t('goal.activeWaiting', { rounds });
    actions = (
      <>
        <GoalAction
          disabled={busy}
          label={t('goal.pause')}
          hint={t('goal.pauseHint')}
          onClick={() => void run(() => pauseAgentGoal(threadId))}
        >
          <PauseIcon />
        </GoalAction>
        {/* Editing is not a stop. A round already running finishes on the
            objective it started with, and only the rounds after it read the
            new words. */}
        <GoalAction
          disabled={busy}
          label={t('goal.edit')}
          hint={t('goal.editHint')}
          onClick={() => setEditing(true)}
        >
          <EditIcon />
        </GoalAction>
        <GoalAction
          disabled={busy}
          label={t('goal.clear')}
          hint={t('goal.clearHint')}
          onClick={() => void run(() => clearAgentGoal(threadId))}
        >
          <ClearIcon />
        </GoalAction>
      </>
    );
  } else if (goal.state === 'paused') {
    // The reason and the platform's own sentence are separate nodes: a template
    // with a hole for the detail renders a dangling separator when there is
    // none.
    text = (
      <>
        {t(goal.pauseReason === 'fork' ? 'goal.pausedFork' : 'goal.paused', {
          reason: t(pauseKey(goal.pauseReason)),
        })}
        {goal.pauseDetail ? ` · ${goal.pauseDetail}` : ''}
      </>
    );
    if (spent) {
      hint = t('goal.limitResumeLifts', { max: goal.maxRounds ?? 0 });
    } else if (state.live) {
      // Pausing stops what comes next, not what is already running. Saying so
      // on the band is the difference between an owner waiting for the round to
      // land and an owner believing the button did nothing.
      hint = t('goal.pausedRunning');
    }
    actions = (
      <>
        <GoalAction
          disabled={busy}
          tone="primary"
          label={t('goal.resume')}
          hint={spent ? t('goal.limitResumeLiftsHint') : t('goal.resumeHint')}
          onClick={() => void run(() => resumeAgentGoal(threadId))}
        >
          <ResumeIcon />
        </GoalAction>
        <GoalAction
          disabled={busy}
          label={t('goal.edit')}
          hint={t('goal.editHint')}
          onClick={() => setEditing(true)}
        >
          <EditIcon />
        </GoalAction>
        <GoalAction
          disabled={busy}
          label={t('goal.clear')}
          hint={t('goal.clearHint')}
          onClick={() => void run(() => clearAgentGoal(threadId))}
        >
          <ClearIcon />
        </GoalAction>
      </>
    );
  } else {
    // The platform never ran the objective itself. A first-version completion
    // is the model's own report, and the band is the one place that says so.
    text =
      goal.completionBasis === 'model_reported'
        ? t('goal.completeReported', { rounds })
        : t('goal.complete', { rounds });
    actions = (
      <GoalAction
        disabled={busy}
        label={t('goal.clear')}
        hint={t('goal.clearHint')}
        onClick={() => void run(() => clearAgentGoal(threadId))}
      >
        <ClearIcon />
      </GoalAction>
    );
  }

  return (
    <div className="mb-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-600 dark:border-white/10 dark:bg-gray-900 dark:text-gray-300">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <GoalIcon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
        {/* Named, because the page has a second status region — the
            conversation's own navigation notice — and an unnamed pair
            announces as two anonymous "status" updates.

            No `basis-48` on it either: a flex basis is what a wrapping row
            breaks on, so one long status sentence would push the controls onto
            their own line above the objective. With a basis of zero the
            sentence wraps inside its own box instead, and the controls stay
            where the design puts them — the end of the first line. */}
        <span role="status" aria-label={t('attach.goal')} className="min-w-0 flex-1">
          {text}
        </span>
        <span className="flex shrink-0 items-center gap-0.5">{actions}</span>
      </div>
      <div className="mt-1">
        {editing ? (
          <GoalEditor
            initial={goal.objective}
            limits={limits}
            busy={busy}
            error={error}
            onCancel={() => setEditing(false)}
            onSave={(objective) => {
              // The revision this band was drawn from travels with the write,
              // so an edit from a second tab is refused rather than
              // overwritten. The editor closes only once the write has landed:
              // a refusal has to leave the owner's words where the retry can
              // reach them, and `useGoalWrite` re-reads on a conflict so the
              // retry goes out against the revision that refusal revealed.
              void run(() => writeGoal(threadId, objective, goal.revision)).then((landed) => {
                if (landed) setEditing(false);
              });
            }}
          />
        ) : (
          /* The objective is what the band is about, and a tooltip is not a
             display: an owner looking at a stopped goal needs to read what it
             was working toward. Collapsed it ellipsises to keep the band one
             line tall; opened it wraps, in full. */
          <button
            type="button"
            onClick={() => setExpanded((was) => !was)}
            aria-expanded={expanded}
            title={goal.objective}
            className={`w-full min-w-0 rounded text-left text-[11px] text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 ${
              expanded ? 'whitespace-pre-wrap break-words' : 'truncate'
            }`}
          >
            {goal.objective}
          </button>
        )}
      </div>
      {hint ? <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{hint}</p> : null}
      {/* While the editor is open it carries the refusal beside the words that
          were refused; the band would only be repeating it off-screen. */}
      {error && !editing ? (
        <p role="alert" className="mt-1 text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
    </div>
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
    // The whole objective, so replacing it is one keystroke and appending to it
    // is one arrow key. This is an edit of what is already there, not a blank
    // form.
    control.setSelectionRange(control.value.length, control.value.length);
  }, []);

  const trimmed = draft.trim();
  const tooLong = draft.length > limits.max_objective_chars;
  const canSave = trimmed.length > 0 && !tooLong && !busy;

  return (
    <div className="w-full">
      <label htmlFor={fieldId} className="sr-only">
        {t('goal.objective')}
      </label>
      <textarea
        id={fieldId}
        ref={field}
        rows={3}
        value={draft}
        maxLength={limits.max_objective_chars}
        aria-invalid={tooLong || undefined}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // An IME's Escape cancels the candidate it is showing, not the edit.
          // The composers' own guard reads the same two fields for Enter.
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === 'Escape') {
            // Consumed here rather than left to bubble: the job page stops the
            // run on Escape, and abandoning an edit must not also stop the run.
            event.preventDefault();
            event.stopPropagation();
            onCancel();
            return;
          }
          // The objective is prose and may run to several lines, so Enter is a
          // newline here. Cmd/Ctrl+Enter is the chord that saves.
          if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            if (canSave) onSave(trimmed);
          }
        }}
        className="w-full resize-y rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-[11px] text-gray-800 focus:border-gray-400 focus:outline-none dark:border-white/10 dark:text-gray-100 dark:focus:border-white/30"
      />
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!canSave}
          onClick={() => onSave(trimmed)}
          className="inline-flex items-center rounded-md bg-gray-900 px-2 py-1 text-[11px] font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
        >
          {busy ? t('goal.editSaving') : t('goal.editSave')}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="inline-flex items-center rounded-md border border-gray-200 px-2 py-1 text-[11px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5"
        >
          {t('goal.editCancel')}
        </button>
        {tooLong ? (
          <span role="alert" className="text-red-600 dark:text-red-400">
            {t('goal.objectiveTooLong', { max: limits.max_objective_chars })}
          </span>
        ) : null}
        {error ? (
          <span role="alert" className="text-red-600 dark:text-red-400">
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
      className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        tone === 'primary'
          ? 'text-crimson hover:bg-crimson/10 dark:text-crimson-light dark:hover:bg-crimson/15'
          : 'text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200'
      }`}
    >
      {children}
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

function EditIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      aria-hidden="true"
      className={ICON}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 20h4.2L19 9.2a2.3 2.3 0 0 0-3.2-3.2L5 16.8V20Zm9.6-13.6 3.2 3.2"
      />
    </svg>
  );
}

function ClearIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      aria-hidden="true"
      className={ICON}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4.5 7h15m-10 4v6m5-6v6M6.5 7l.9 13h9.2l.9-13M9.5 7V4h5v3"
      />
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
