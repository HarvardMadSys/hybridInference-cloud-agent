'use client';

import { useState } from 'react';
import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal } from '@/lib/api/agents';
import type { AgentGoal } from '../types';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { GoalIcon } from '../ComposerPlusMenu';
import { GoalDialog, type GoalDraft } from './GoalDialog';
import { writeGoal } from './goalApi';
import { useGoalWrite } from './useGoalWrite';
import type { GoalLimits } from '@/lib/goalLimits';

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
 * The controls are icons in one cluster at the end, and each one carries both
 * a name and a sentence: the name is what a screen reader says, the sentence is
 * what a pointer explains, and each sentence is the operation's own semantics —
 * that Pause leaves the running round alone, that Edit does not restart what
 * has already been queued, and that Clear takes the goal and not the chat.
 */
export function GoalBar({
  threadId,
  goal,
  limits,
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

  const rounds = t('goal.rounds', { used: goal.roundsUsed, max: goal.maxRounds });
  // A paused goal with nothing left to spend is the one stop the owner cannot
  // clear from here: the server refuses to resume it, so the band says what has
  // to happen first rather than offering a button that can only fail.
  //
  // Read off the numbers, never off `pause_reason`. `round_limit` stays on the
  // row after the cap is raised — it is the history of why the goal stopped,
  // not a standing verdict — and a band that refused on the reason alone would
  // keep Resume disabled for a goal the server would now happily continue.
  const spent = goal.roundsUsed >= goal.maxRounds;

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
      // The cap is what stopped it, and the cap is what has to move. Resume
      // stays visible but refused, so the way forward is the button beside it.
      hint = t('goal.limitReached', { max: goal.maxRounds });
    } else if (state.live) {
      // Pausing stops what comes next, not what is already running. Saying so
      // on the band is the difference between an owner waiting for the round to
      // land and an owner believing the button did nothing.
      hint = t('goal.pausedRunning');
    }
    actions = (
      <>
        <GoalAction
          disabled={busy || spent}
          tone="primary"
          label={t('goal.resume')}
          hint={spent ? t('goal.limitReachedHint') : t('goal.resumeHint')}
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
    <div
      role="status"
      // Named, because the page has a second status region — the conversation's
      // own navigation notice — and an unnamed pair announces as two anonymous
      // "status" updates.
      aria-label={t('attach.goal')}
      className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-600 dark:border-white/10 dark:bg-gray-900 dark:text-gray-300"
    >
      <GoalIcon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
      {/* No `basis-48` on the status: a flex basis is what a wrapping row
          breaks on, so one long status sentence would push the controls onto
          their own line above the objective on a narrow screen. With a basis
          of zero the sentence wraps inside its own box instead, and the
          controls stay where the design puts them — the end of the first
          line. */}
      <span className="min-w-0 flex-1">{text}</span>
      <span className="flex shrink-0 items-center gap-0.5">{actions}</span>
      {/* The objective is what the band is about, and a tooltip is not a
          display: an owner looking at a stopped goal needs to read what it was
          working toward. Collapsed it ellipsises to keep the band one line tall;
          opened it wraps, in full. */}
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
      {hint ? (
        <span className="w-full text-[11px] text-gray-500 dark:text-gray-400">{hint}</span>
      ) : null}
      {/* While the dialog is open it carries the refusal beside the words that
          were refused; the band would only be repeating it off-screen. */}
      {error && !editing ? (
        <span role="alert" className="w-full text-red-600 dark:text-red-400">
          {error}
        </span>
      ) : null}
      <GoalDialog
        open={editing}
        limits={limits}
        initial={{ objective: goal.objective, maxRounds: goal.maxRounds }}
        busy={busy}
        error={editing ? error : null}
        onClose={() => setEditing(false)}
        onApply={(draft: GoalDraft) => {
          // The revision this band was drawn from travels with the write, so an
          // edit from a second tab is refused rather than overwritten. The
          // dialog closes only once the write has landed: a refusal has to
          // leave the owner's words where the retry can reach them.
          void run(() => writeGoal(threadId, draft, goal.revision)).then((landed) => {
            if (landed) setEditing(false);
          });
        }}
        onClear={() => {
          void run(() => clearAgentGoal(threadId)).then((landed) => {
            if (landed) setEditing(false);
          });
        }}
      />
    </div>
  );
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
