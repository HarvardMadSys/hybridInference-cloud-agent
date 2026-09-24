import type { KeyboardEvent } from 'react';

/** What browsers report for a keypress an IME is still holding. */
const IME_KEY_CODE = 229;

/**
 * Whether a composer keypress means "send" rather than "newline".
 *
 * Both composers were bare textareas with no key handler at all, so the only
 * way to start a run was to travel to the button: the one interaction every
 * chat surface is expected to have was missing, and Enter, Shift+Enter and
 * Cmd+Enter alike just broke the line. Enter sends and Shift+Enter breaks the
 * line, matching the composers people arrive from; Cmd/Ctrl+Enter is accepted
 * as the other chord they arrive with, and Alt+Enter stays a newline.
 *
 * An IME's Enter commits the candidate it is showing, and the browser delivers
 * that keypress to the page like any other. Reading it as "send" would fire the
 * message on the first Chinese, Japanese or Korean word typed, so a keypress
 * made while composing is never a submit. Some browsers report the commit
 * without setting `isComposing`, hence the legacy keyCode alongside it.
 *
 * A submit keypress is always consumed, including when the composer has nothing
 * to send: Enter is the send key here and never the newline, so on an empty box
 * it does nothing at all rather than growing a blank line. Shift+Enter remains
 * the way to break a line in any state.
 */
export function isComposerSubmit(event: KeyboardEvent<HTMLElement>): boolean {
  if (event.key !== 'Enter') return false;
  if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === IME_KEY_CODE) return false;
  return !event.shiftKey && !event.altKey;
}

/**
 * Cmd/Ctrl+Enter: send into the running turn now rather than after it.
 *
 * Only where a running turn can take a message does it mean anything more than
 * Enter; the caller decides that. An IME's Enter is never this chord either.
 */
export function isSteerChord(event: KeyboardEvent<HTMLElement>): boolean {
  if (event.key !== 'Enter') return false;
  if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === IME_KEY_CODE) return false;
  return (event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey;
}
