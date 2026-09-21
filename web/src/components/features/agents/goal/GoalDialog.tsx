'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { DEFAULT_GOAL_LIMITS, type GoalLimits } from '@/lib/goalLimits';
import { useT } from '@/components/providers';
import { SKILL_BUTTON, SKILL_FIELD, SKILL_PRIMARY } from '../skills/SkillDialog';

/** The completion condition this draft asks for. */
export interface GoalDraft {
  objective: string;
  maxRounds: number;
}

/**
 * Set, edit or clear one conversation's completion goal.
 *
 * It edits local state and nothing else: the composer carries the draft on the
 * message the user is about to send, and the job view writes it straight away.
 * That split is deliberate — a goal with no message attached has nothing to
 * work on, so the composer must not create one on its own.
 *
 * The portal, the focus trap and the Escape handling mirror ``SkillDialog``
 * rather than reusing it. That component is titled and labelled for skills, and
 * sharing it would mean parameterising every one of its strings; the two are
 * twenty lines of identical structure and a lifetime of "which dialog is this?"
 */
export function GoalDialog({
  open,
  initial,
  limits = DEFAULT_GOAL_LIMITS,
  busy = false,
  error = null,
  onClose,
  onApply,
  onClear,
}: {
  open: boolean;
  initial?: GoalDraft | null;
  limits?: GoalLimits;
  /** A write is in flight; the buttons stand down until it lands. */
  busy?: boolean;
  /**
   * Why the last attempt failed.
   *
   * The dialog stays open on a refusal, so this is where the refusal is read —
   * and the draft is still in the box beside it, because the retry is the same
   * words. Closing on the way out would throw away what the owner just typed
   * for a write that never happened.
   */
  error?: string | null;
  onClose: () => void;
  onApply: (goal: GoalDraft) => void;
  /** Absent when there is nothing to clear — a goal that was never set. */
  onClear?: () => void;
}) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const [objective, setObjective] = useState(initial?.objective ?? '');
  const [rounds, setRounds] = useState(String(initial?.maxRounds ?? limits.default_rounds));
  const wasOpen = useRef(false);

  // Opening shows what the goal says now, not the last draft.
  //
  // On the *transition* into open, and never again while it stays open. The
  // caller's `initial` moves under this dialog whenever the page re-reads the
  // conversation — a compare-and-set refusal makes it re-read by design — and
  // resetting on that would type the other tab's goal over the words this
  // owner is holding. Those words are the retry: the new revision is the only
  // thing that re-read is for. They are replaced when the owner closes the
  // dialog and opens it again, which is how a draft is abandoned.
  useEffect(() => {
    if (!open) {
      wasOpen.current = false;
      return;
    }
    if (wasOpen.current) return;
    wasOpen.current = true;
    setObjective(initial?.objective ?? '');
    setRounds(String(initial?.maxRounds ?? limits.default_rounds));
  }, [open, initial?.objective, initial?.maxRounds, limits.default_rounds]);

  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLElement>('textarea')?.focus();
    return () => {
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);

  if (!open || typeof document === 'undefined') return null;

  const trimmed = objective.trim();
  const parsed = Number.parseInt(rounds, 10);
  const roundsValid = Number.isInteger(parsed) && parsed >= 1 && parsed <= limits.max_rounds;
  const tooLong = objective.length > limits.max_objective_chars;
  const canApply = trimmed.length > 0 && roundsValid && !tooLong;

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          onClose();
        }
        if (event.key === 'Tab') {
          const controls = Array.from(
            panel.current?.querySelectorAll<HTMLElement>(
              'button:not(:disabled), input:not(:disabled), textarea:not(:disabled)',
            ) ?? [],
          ).filter((element) => !element.closest('[hidden]'));
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (first && last && event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (first && last && !event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }
      }}
    >
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('goal.close')}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 px-6 pt-5 pb-4">
          <div className="min-w-0">
            <h2
              id={titleId}
              className="break-words text-lg font-semibold text-gray-900 dark:text-gray-100"
            >
              {t('goal.dialog.title')}
            </h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {t('goal.dialog.description')}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('goal.close')}
            className="shrink-0 rounded-md px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5"
          >
            {t('goal.close')}
          </button>
        </div>

        <div className="min-h-0 overflow-y-auto px-6 pb-5">
          <label className="block text-sm font-medium text-gray-800 dark:text-gray-200">
            {t('goal.dialog.objective')}
            <textarea
              rows={4}
              value={objective}
              maxLength={limits.max_objective_chars}
              onChange={(event) => setObjective(event.target.value)}
              placeholder={t('goal.dialog.objectivePlaceholder')}
              className={`${SKILL_FIELD} resize-none`}
            />
          </label>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {t('goal.dialog.objectiveHint')}
          </p>

          <label className="mt-4 block text-sm font-medium text-gray-800 dark:text-gray-200">
            {t('goal.dialog.rounds')}
            <input
              type="number"
              min={1}
              max={limits.max_rounds}
              value={rounds}
              onChange={(event) => setRounds(event.target.value)}
              className={`${SKILL_FIELD} max-w-24`}
            />
          </label>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {t('goal.dialog.roundsHint', { max: limits.max_rounds })}
          </p>

          {tooLong ? (
            <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
              {t('goal.dialog.tooLong', { max: limits.max_objective_chars })}
            </p>
          ) : null}

          {/* The refusal, beside the words it refused. Dismissed with the
              dialog, because cancelling is how a person abandons a retry. */}
          {error ? (
            <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-gray-100 px-6 py-4 dark:border-white/5">
          {onClear ? (
            <button
              type="button"
              disabled={busy}
              onClick={onClear}
              className={`${SKILL_BUTTON} mr-auto text-red-600 dark:text-red-400 disabled:opacity-50`}
            >
              {t('goal.dialog.clear')}
            </button>
          ) : null}
          <button type="button" onClick={onClose} className={SKILL_BUTTON}>
            {t('goal.dialog.cancel')}
          </button>
          <button
            type="button"
            // Disabled while a write is in flight, so one draft is one write.
            disabled={!canApply || busy}
            onClick={() => {
              if (!canApply || busy) return;
              onApply({ objective: trimmed, maxRounds: parsed });
            }}
            className={SKILL_PRIMARY}
          >
            {busy ? t('goal.dialog.applying') : t('goal.dialog.apply')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
