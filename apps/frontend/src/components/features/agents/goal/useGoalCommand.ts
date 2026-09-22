'use client';

import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import type { Dispatch, KeyboardEvent, SetStateAction } from 'react';
import { useT } from '@/components/providers';
import { isComposerSubmit } from '../composerKeys';
import {
  composerCommandMatches,
  parseGoalCommand,
  type ComposerCommand,
} from '../composerCommands';

/** The text a chosen Goal row leaves in the box, ready for the objective. */
export const GOAL_COMMAND_PREFIX = '/goal ';

/**
 * What the composer has to hand the hook for a command to be usable.
 *
 * The two composers differ in exactly two places — what "set this goal" means
 * when there is no conversation yet, and where a missing objective is reported
 * — so those are the two things they pass in. Everything else about the
 * command, including the rule for what is one, is the same on both.
 */
export interface GoalCommandWiring {
  /** The text in the box right now. */
  value: string;
  /**
   * Replace it, so a consumed command leaves nothing behind.
   *
   * The box's own setter, updater form included: a command that is cleared
   * only after its write lands has to look at what the box holds *then*, and
   * the owner may have started typing the next message in the meantime.
   */
  setValue: Dispatch<SetStateAction<string>>;
  /**
   * Focus the message box.
   *
   * Choosing the Goal row *writes into* the box rather than opening anything,
   * so the caret has to end up back where the next character goes — on the
   * same line, after the token, with no second click.
   */
  focusComposer: () => void;
  /**
   * The box held `/goal` and nothing else.
   *
   * Not an objective, so nothing is created and nothing is sent to the model:
   * the composer says what is missing and leaves the token in the box for the
   * owner to finish.
   */
  onNeedsObjective: () => void;
  /** Apply `/goal <objective>`; a rejection is reported, never swallowed. */
  onApply: (objective: string) => Promise<void>;
  /** Put a refused write where the composer shows its other failures. */
  onError: (message: string) => void;
}

/** Everything the composer needs to draw the menu and run what it holds. */
export interface GoalCommandControls {
  /** The rows to draw. Empty means no menu. */
  entries: ComposerCommand[];
  /** Whether the menu is showing. */
  open: boolean;
  /** The id the box points its `aria-activedescendant` at. */
  menuId: string;
  activeIndex: number;
  /** The highlighted row's id, or undefined while nothing is highlighted. */
  activeId: string | undefined;
  /** Highlighted row, so a pointer can move the selection Enter would take. */
  onHighlight: (index: number) => void;
  /** Take the highlighted row, as Enter would. */
  onChoose: (index: number) => void;
  /** True while a `/goal <objective>` write is in flight. */
  busy: boolean;
  /** Take a key while the menu is open; true means it was consumed. */
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Run the command the box holds. True when the box was a command at all. */
  submit: () => Promise<boolean>;
}

/**
 * The `/goal` command, for either composer.
 *
 * The whole interaction is the message box. Choosing the row leaves `/goal ` in
 * it and puts the caret after the space, so the objective is typed where it
 * would have been typed anyway; Enter takes the row rather than sending half a
 * command to the model, and the IME guard is the composers' own, so an Enter
 * that commits a candidate is never a submit.
 *
 * A command carrying an objective is submitted in one step — one message, one
 * task, one goal — and the box is cleared only if it still holds that command.
 */
export function useGoalCommand({
  value,
  setValue,
  focusComposer,
  onNeedsObjective,
  onApply,
  onError,
}: GoalCommandWiring): GoalCommandControls {
  const t = useT();
  const menuId = useId();
  const [activeIndex, setActiveIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [busy, setBusy] = useState(false);
  const matches = useMemo(() => composerCommandMatches(value), [value]);
  const open = matches.length > 0 && !dismissed;
  // What the menu draws: nothing at all once it has been dismissed, which is
  // what makes Escape mean "not now" rather than "keep showing it".
  const entries = open ? matches : [];

  // Escape hides the menu until the next keystroke. Typing is what asks for it
  // again, so the hiding must not outlive the text it was about.
  useEffect(() => setDismissed(false), [value]);

  // One command today. Choosing it writes the token into the box and hands the
  // caret back, and the table is what says so: a second command with its own
  // behaviour is a switch here, not a second menu.
  const choose = useCallback(
    (index: number) => {
      if (!matches[index]) return;
      setDismissed(true);
      setValue(GOAL_COMMAND_PREFIX);
      focusComposer();
    },
    [focusComposer, matches, setValue],
  );

  const onHighlight = useCallback((index: number) => setActiveIndex(index), []);

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
    if (!open) return false;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % matches.length);
      return true;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + matches.length) % matches.length);
      return true;
    }
    if (event.key === 'Escape') {
      // Consumed here rather than left to bubble: the job page stops the run on
      // Escape, and dismissing a menu must not also stop the run.
      event.preventDefault();
      event.stopPropagation();
      setDismissed(true);
      return true;
    }
    if (isComposerSubmit(event)) {
      event.preventDefault();
      choose(activeIndex);
      return true;
    }
    return false;
  }

  async function submit(): Promise<boolean> {
    // A second Enter while the write is in flight is not a second write.
    if (busy) return true;
    // A menu still showing means the command has not been chosen yet, whatever
    // the box holds: `/go` is not a request. Enter and the send button agree.
    if (open) {
      choose(activeIndex);
      return true;
    }
    const command = parseGoalCommand(value);
    if (!command) return false;
    if (command.kind === 'empty') {
      // Consumed, and deliberately not cleared: the token is the beginning of
      // the sentence the owner is about to finish.
      onNeedsObjective();
      return true;
    }
    const submitted = value;
    setBusy(true);
    try {
      await onApply(command.objective);
      // Consumed: the objective is a goal now, and the box must not also send
      // it as a message. On failure the text stays, because the retry is the
      // same command.
      //
      // Only if the box still holds that command, though. A write takes a
      // round trip, and the owner may have started the next message in it —
      // clearing then would delete something this command never owned.
      setValue((current) => (current === submitted ? '' : current));
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : t('goal.commandFailed'));
    } finally {
      setBusy(false);
    }
    return true;
  }

  return {
    entries,
    open,
    menuId,
    activeIndex,
    activeId: open && matches[activeIndex] ? `${menuId}-${matches[activeIndex].name}` : undefined,
    onHighlight,
    onChoose: choose,
    busy,
    onKeyDown,
    submit,
  };
}
