'use client';

import { useState } from 'react';
import { clearAgentGoal, pauseAgentGoal, resumeAgentGoal, setAgentGoal } from '@/lib/api/agents';
import type { AgentGoal } from '../types';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { GoalIcon } from '../ComposerPlusMenu';
import { GoalDialog, type GoalDraft } from './GoalDialog';
import type { GoalLimits } from '@/lib/goalLimits';

/** What the band needs that the goal row does not carry. */
export interface GoalBandState {
  /** A turn of this conversation is queued or running right now. */
  live: boolean;
  /** The turn in flight is a verification round rather than work. */
  verifying: boolean;
  /** The most recent turn's report, for the "last round:" line. */
  lastReason?: string;
  /** A fork's goal never continues on its own until the owner says so. */
  forked?: boolean;
}

/**
 * The goal's status, above the message box, and the only place it is managed.
 *
 * Five shapes, one per state the platform can put a goal in, because an owner
 * looking at a stopped conversation needs to know *which* stop it was: paused
 * by them, forked, out of rounds, blocked by the model, or done. A single
 * "inactive" band would hide the one fact that decides what they do next.
 *
 * The band is absent, not empty, when there is no goal. A conversation that is
 * simply a conversation must look like one.
 */
export function GoalBar({
  jobId,
  goal,
  limits,
  state,
  onChanged,
}: {
  jobId: string;
  goal: AgentGoal;
  limits?: GoalLimits;
  state: GoalBandState;
  /**
   * Called after a write lands. The server answers with the whole job row; a
   * caller that already knows how to reload one passes that, and this component
   * deliberately does not care which.
   */
  onChanged: (job: unknown) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<{ job: unknown }>) {
    setBusy(true);
    setError(null);
    try {
      onChanged((await action()).job);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('goal.actionFailed'));
    } finally {
      setBusy(false);
    }
  }

  const rounds = t('goal.rounds', { used: goal.roundsUsed, max: goal.maxRounds });
  const reason = goal.blockedMessage ?? state.lastReason;

  let text: React.ReactNode;
  let actions: React.ReactNode;
  if (goal.state === 'active') {
    // Between rounds the platform is not idle, it is waiting for its own next
    // turn to be claimed — often because a person's message is queued ahead of
    // it. The label says which, so "no round running" is never mistaken for
    // "the goal stopped".
    text = state.verifying
      ? t('goal.activeVerifying', { rounds })
      : state.live
        ? t('goal.active', { rounds })
        : t('goal.activeWaiting', { rounds });
    actions = (
      <BandButton disabled={busy} onClick={() => void run(() => pauseAgentGoal(jobId))}>
        {t('goal.pause')}
      </BandButton>
    );
  } else if (goal.state === 'paused') {
    text = t(state.forked ? 'goal.pausedFork' : 'goal.paused', {
      reason: t(pauseKey(goal.pauseReason)),
    });
    actions = (
      <>
        <BandButton
          disabled={busy}
          onClick={() => void run(() => resumeAgentGoal(jobId))}
          tone="primary"
        >
          {t('goal.resume')}
        </BandButton>
        <BandButton disabled={busy} onClick={() => setEditing(true)}>
          {t('goal.edit')}
        </BandButton>
        <BandButton disabled={busy} onClick={() => void run(() => clearAgentGoal(jobId))}>
          {t('goal.clear')}
        </BandButton>
      </>
    );
  } else if (goal.state === 'blocked') {
    // The reason and the model's own words are separate nodes: a template with
    // a hole for the detail renders a dangling separator when there is none.
    text = (
      <>
        {t('goal.blocked', {
          reason: t(
            goal.blockedCode === 'round-limit' ? 'goal.blockedRoundLimit' : 'goal.blockedModel',
          ),
        })}
        {reason ? ` · ${reason}` : ''}
      </>
    );
    actions = (
      <>
        <BandButton disabled={busy} onClick={() => setEditing(true)} tone="primary">
          {t('goal.edit')}
        </BandButton>
        <BandButton disabled={busy} onClick={() => void run(() => clearAgentGoal(jobId))}>
          {t('goal.clear')}
        </BandButton>
      </>
    );
  } else {
    text = t('goal.complete', { rounds });
    actions = (
      <BandButton disabled={busy} onClick={() => void run(() => clearAgentGoal(jobId))}>
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
      <span className="min-w-0 flex-1 basis-48 truncate" title={goal.objective}>
        {text}
      </span>
      {actions}
      {error ? (
        <span role="alert" className="w-full text-red-600 dark:text-red-400">
          {error}
        </span>
      ) : null}
      <GoalDialog
        open={editing}
        limits={limits}
        initial={{ objective: goal.objective, maxRounds: goal.maxRounds }}
        onClose={() => setEditing(false)}
        onApply={(draft: GoalDraft) => {
          setEditing(false);
          void run(() =>
            setAgentGoal(jobId, {
              goal: { objective: draft.objective, max_rounds: draft.maxRounds },
              revision: goal.revision,
            }),
          );
        }}
        onClear={() => {
          setEditing(false);
          void run(() => clearAgentGoal(jobId));
        }}
      />
    </div>
  );
}

function BandButton({
  children,
  onClick,
  disabled,
  tone = 'plain',
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: 'plain' | 'primary';
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
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
  user: 'goal.reasonUser',
  fork: 'goal.reasonFork',
  'turn-failed': 'goal.reasonFailed',
  cancelled: 'goal.reasonCancelled',
  'context-lost': 'goal.reasonContextLost',
  archived: 'goal.reasonArchived',
  stalled: 'goal.reasonStalled',
};

/** An unrecognised reason is still a reason: say it stopped, not what it was. */
function pauseKey(reason: string | null | undefined): TranslationKey {
  return (reason && PAUSE_KEYS[reason]) || 'goal.reasonUnknown';
}
