'use client';

import { useState } from 'react';
import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal, setAgentGoal } from '@/lib/api/agents';
import type { AgentGoal } from '../types';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { APIError } from '@/lib/utils/errors';
import { GoalIcon } from '../ComposerPlusMenu';
import { GoalDialog, type GoalDraft } from './GoalDialog';
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('goal.actionFailed'));
      // A compare-and-set refusal means this band is rendering a revision the
      // goal has already moved past. Reading it back is the only way Edit gets
      // anywhere from here: the next write would carry the same stale revision.
      if (cause instanceof APIError && cause.code === 'goal_revision_conflict') onChanged();
    } finally {
      setBusy(false);
    }
  }

  const rounds = t('goal.rounds', { used: goal.roundsUsed, max: goal.maxRounds });

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
      <BandButton
        disabled={busy}
        title={t('goal.pauseHint')}
        onClick={() => void run(() => pauseAgentGoal(threadId))}
      >
        {t('goal.pause')}
      </BandButton>
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
    // Pausing stops what comes next, not what is already running. Saying so on
    // the band is the difference between an owner waiting for the round to land
    // and an owner believing the button did nothing.
    if (state.live) hint = t('goal.pausedRunning');
    actions = (
      <>
        <BandButton
          disabled={busy}
          onClick={() => void run(() => resumeAgentGoal(threadId))}
          tone="primary"
        >
          {t('goal.resume')}
        </BandButton>
        <BandButton disabled={busy} onClick={() => setEditing(true)}>
          {t('goal.edit')}
        </BandButton>
        <BandButton disabled={busy} onClick={() => void run(() => clearAgentGoal(threadId))}>
          {t('goal.clear')}
        </BandButton>
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
      <BandButton disabled={busy} onClick={() => void run(() => clearAgentGoal(threadId))}>
        {t('goal.clear')}
      </BandButton>
    );
  }

  return (
    <div
      role="status"
      className="mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-600 dark:border-white/10 dark:bg-gray-900 dark:text-gray-300"
    >
      <GoalIcon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
      <span className="min-w-0 flex-1 basis-48 truncate">{text}</span>
      {actions}
      {error ? (
        <span role="alert" className="w-full text-red-600 dark:text-red-400">
          {error}
        </span>
      ) : null}
      {/* The objective is what the band is about, and a tooltip is not a
          display: an owner looking at a stopped goal needs to read what it was
          working toward. */}
      <span
        className="w-full min-w-0 truncate text-[11px] text-gray-500 dark:text-gray-400"
        title={goal.objective}
      >
        {goal.objective}
      </span>
      {hint ? (
        <span className="w-full text-[11px] text-gray-500 dark:text-gray-400">{hint}</span>
      ) : null}
      <GoalDialog
        open={editing}
        limits={limits}
        initial={{ objective: goal.objective, maxRounds: goal.maxRounds }}
        onClose={() => setEditing(false)}
        onApply={(draft: GoalDraft) => {
          setEditing(false);
          void run(() =>
            setAgentGoal(threadId, {
              goal: { objective: draft.objective, max_rounds: draft.maxRounds },
              revision: goal.revision,
            }),
          );
        }}
        onClear={() => {
          setEditing(false);
          void run(() => clearAgentGoal(threadId));
        }}
      />
    </div>
  );
}

function BandButton({
  children,
  onClick,
  disabled,
  title,
  tone = 'plain',
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  tone?: 'plain' | 'primary';
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`shrink-0 rounded-md px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 ${
        tone === 'primary'
          ? 'bg-gray-900 text-white hover:bg-gray-700 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white'
          : 'border border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/5'
      }`}
    >
      {children}
    </button>
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
