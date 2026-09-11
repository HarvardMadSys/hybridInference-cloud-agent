'use client';

// The conversation's turn rail, its directory panel, and the way back to the
// newest reply.
//
// Presentation only: every component here receives the entries and asks for a
// jump. Where the jump lands, whether the transcript follows new output and
// which mark is current are `useConversationScroll`'s to decide, which is what
// keeps a hover preview from moving the transcript and a click from fighting
// the stream.

import { memo, useCallback, useEffect, useId, useRef, useState, type CSSProperties } from 'react';

import { useT } from '@/components/providers';

import type { ConversationTurnNavigationEntry } from './conversationTurnNavigation';

/**
 * Fixed distance between neighbouring marks, and the height of each mark's hit
 * area. Marks never compress to fit: a conversation taller than the rail
 * scrolls inside it, so a mark stays a 32x24 target instead of becoming a
 * hairline.
 */
const TURN_PITCH_PX = 24;
/** Height of the gradient fade marking a scrollable end of the rail. */
const FADE_PX = 24;
/** Tallest the rail may grow; the band it sits in can still be shorter. */
const RAIL_MAX_HEIGHT_PX = 420;
/** Visual gap between the rail and the card that previews a mark. */
const PREVIEW_GAP_PX = 8;
/**
 * Grace period for the pointer to travel from a mark to the card. The card is
 * not a child of the mark, so any movement between them is a leave followed by
 * an enter, and closing on the leave alone makes the card impossible to reach.
 */
const PREVIEW_CLOSE_DELAY_MS = 150;
const MARK_HIT_WIDTH_PX = 32;
const PREVIEW_WIDTH_PX = 300;

interface TurnNavigationProps {
  entries: readonly ConversationTurnNavigationEntry[];
  activeKey: string | null;
  onNavigate: (key: string) => void;
}

const MARK_WIDTH_PX = { resting: 12, preview: 18, active: 20 } as const;
/** Nominal card height, for keeping the card inside the rail's band. */
const PREVIEW_NOMINAL_HEIGHT_PX = 96;

const STATE_KEYS = {
  queued: 'turnNavigation.state.queued',
  running: 'turnNavigation.state.running',
  needs_review: 'turnNavigation.state.needsReview',
  done: 'turnNavigation.state.done',
  failed: 'turnNavigation.state.failed',
  cancelled: 'turnNavigation.state.cancelled',
} as const;

function markWidth(active: boolean, preview: boolean): number {
  if (active) return MARK_WIDTH_PX.active;
  if (preview) return MARK_WIDTH_PX.preview;
  return MARK_WIDTH_PX.resting;
}

/** Vertical centre of the mark for a turn, in the rail's content coordinates. */
function markCenter(index: number): number {
  return index * TURN_PITCH_PX + TURN_PITCH_PX / 2;
}

/** Where the preview card sits, clamped inside the rail's visible band. */
function previewOffset(index: number, scrollTop: number, railHeight: number): number {
  const centre = markCenter(index) - scrollTop;
  const lowest = Math.max(0, railHeight - PREVIEW_NOMINAL_HEIGHT_PX);
  return Math.max(0, Math.min(centre - PREVIEW_NOMINAL_HEIGHT_PX / 2, lowest));
}

function railMask(canScrollUp: boolean, canScrollDown: boolean): CSSProperties {
  if (!canScrollUp && !canScrollDown) return {};
  const top = `transparent 0, #000 ${FADE_PX}px`;
  const bottom = `#000 calc(100% - ${FADE_PX}px), transparent 100%`;
  const gradient =
    canScrollUp && canScrollDown
      ? `linear-gradient(to bottom, ${top}, ${bottom})`
      : canScrollUp
        ? `linear-gradient(to bottom, ${top})`
        : `linear-gradient(to bottom, ${bottom})`;
  return { maskImage: gradient, WebkitMaskImage: gradient };
}

function TurnPreview({ entry }: { entry: ConversationTurnNavigationEntry }) {
  const t = useT();
  return (
    <>
      <div className="line-clamp-1 text-[13px] font-medium text-gray-900 dark:text-gray-100">
        {entry.promptPreview || t('turnNavigation.turn', { turn: entry.displayNo })}
      </div>
      {entry.responsePreview ? (
        <div className="mt-1 line-clamp-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
          {entry.responsePreview}
        </div>
      ) : null}
      <div className="mt-1.5 flex items-center gap-2 text-[11px] text-gray-400 dark:text-gray-500">
        <span>{t('turnNavigation.turn', { turn: entry.displayNo })}</span>
        {entry.previewAttemptNo === undefined ? null : (
          <span>{t('turnNavigation.attempt', { attempt: entry.previewAttemptNo })}</span>
        )}
        {entry.state === undefined ? null : <span>{t(STATE_KEYS[entry.state])}</span>}
      </div>
    </>
  );
}

interface TurnRailProps extends TurnNavigationProps {
  /** Band the rail may occupy; JobDetail sizes it to the reading area. */
  maxHeightPx?: number;
}

/**
 * The rail of marks beside the transcript.
 *
 * Hidden below two turns: a ladder with one rung is not navigation. Its own
 * scrolling is contained, so a flick over the rail never moves the Chat.
 */
function TurnRailRail({
  entries,
  activeKey,
  onNavigate,
  maxHeightPx = RAIL_MAX_HEIGHT_PX,
}: TurnRailProps) {
  const t = useT();
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [scrollLimits, setScrollLimits] = useState({ up: false, down: false });
  const [railHeight, setRailHeight] = useState(0);
  const [focusIndex, setFocusIndex] = useState(0);
  const frameRef = useRef<HTMLElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const markRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const closeTimerRef = useRef<number | null>(null);
  /** While the pointer works the rail, the active mark must not move under it. */
  const pointerInsideRef = useRef(false);
  const focusInsideRef = useRef(false);
  /**
   * Whether the reader is working the rail right now. The mark they are on must
   * not move under the hand or the focus ring, and the ladder must come back to
   * the current turn once they let go of it.
   */
  const [interacting, setInteracting] = useState(false);
  const previewId = useId();

  const syncInteraction = useCallback(() => {
    setInteracting(pointerInsideRef.current || focusInsideRef.current);
  }, []);

  const cancelPreviewClose = useCallback(() => {
    if (closeTimerRef.current === null) return;
    window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  }, []);

  /**
   * Let go of the preview, and of the rail's interaction, after a beat.
   *
   * Closing on the leave itself would end the preview in the gap between the
   * rail and the card, which is the one place the pointer has to cross to read
   * it. Keeping the interaction until the delay elapses also keeps the rail
   * from sliding the current mark under a pointer that is still on the card.
   */
  const schedulePreviewClose = useCallback(() => {
    cancelPreviewClose();
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      pointerInsideRef.current = false;
      syncInteraction();
      setPreviewKey(null);
    }, PREVIEW_CLOSE_DELAY_MS);
  }, [cancelPreviewClose, syncInteraction]);

  useEffect(() => cancelPreviewClose, [cancelPreviewClose]);

  const syncScrollState = useCallback(() => {
    const scroller = scrollerRef.current;
    if (scroller === null) return;
    const top = scroller.scrollTop;
    const up = top > 1;
    const down = top < scroller.scrollHeight - scroller.clientHeight - 1;
    setScrollTop((current) => (current === top ? current : top));
    setScrollLimits((current) =>
      current.up === up && current.down === down ? current : { up, down },
    );
    setRailHeight((current) =>
      current === scroller.clientHeight ? current : scroller.clientHeight,
    );
  }, []);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller === null || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(syncScrollState);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [syncScrollState]);

  useEffect(syncScrollState, [entries.length, syncScrollState]);

  // Tab enters the rail on the turn the reader is already on, rather than on
  // the first turn of the conversation. While the rail holds focus the reader
  // owns the tab stop — arrow keys moved it there — so the reading position
  // must not move it out from under them.
  const activeIndex = entries.findIndex((entry) => entry.key === activeKey);
  useEffect(() => {
    if (activeIndex < 0) return;
    const frame = frameRef.current;
    if (frame !== null && frame.contains(document.activeElement)) return;
    setFocusIndex(activeIndex);
  }, [activeIndex]);

  // Keep the current mark in view, unless the reader's pointer or keyboard is
  // working the rail. The effect re-runs when they let go, which is the moment
  // the current turn has to be brought back into view.
  useEffect(() => {
    const scroller = scrollerRef.current;
    const index = entries.findIndex((entry) => entry.key === activeKey);
    if (scroller === null || index < 0 || interacting) return;
    const markTop = markCenter(index);
    const viewTop = scroller.scrollTop;
    const viewHeight = scroller.clientHeight;
    if (viewHeight <= 0) return;
    if (markTop >= viewTop + FADE_PX && markTop <= viewTop + viewHeight - FADE_PX) return;
    const target = Math.max(0, markTop - viewHeight / 2);
    if (typeof scroller.scrollTo === 'function') {
      try {
        scroller.scrollTo({ top: target, behavior: 'auto' });
      } catch {
        scroller.scrollTop = target;
      }
    } else {
      scroller.scrollTop = target;
    }
    syncScrollState();
  }, [activeKey, entries, interacting, syncScrollState]);

  // Escape closes a preview wherever focus happens to be — a hovered mark never
  // takes focus — and claims the key so that closing a preview cannot also stop
  // the running task, which is what Escape does everywhere else in this view.
  useEffect(() => {
    if (previewKey === null) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setPreviewKey(null);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [previewKey]);

  if (entries.length < 2) return null;

  const previewIndex = entries.findIndex((entry) => entry.key === previewKey);
  const preview = previewIndex < 0 ? undefined : entries[previewIndex];
  const naturalHeight = entries.length * TURN_PITCH_PX;

  const moveFocus = (index: number) => {
    const next = Math.max(0, Math.min(entries.length - 1, index));
    setFocusIndex(next);
    markRefs.current[next]?.focus();
    setPreviewKey(entries[next].key);
  };

  return (
    <nav
      ref={frameRef}
      aria-label={t('turnNavigation.rail')}
      className="pointer-events-auto relative w-8 shrink-0"
      style={{ height: Math.min(naturalHeight, maxHeightPx) }}
      onPointerEnter={() => {
        cancelPreviewClose();
        pointerInsideRef.current = true;
        syncInteraction();
      }}
      onPointerLeave={schedulePreviewClose}
      onFocus={() => {
        focusInsideRef.current = true;
        syncInteraction();
      }}
      onBlur={(event) => {
        // Moving between two marks blurs one and focuses the other; only a
        // focus that left the rail entirely ends the interaction.
        const next = event.relatedTarget instanceof Node ? event.relatedTarget : null;
        if (next !== null && event.currentTarget.contains(next)) return;
        focusInsideRef.current = false;
        syncInteraction();
      }}
      onKeyDown={(event) => {
        // Arrow keys move between marks and nothing else: the transcript is
        // not scrolled until the reader commits with Enter or Space.
        if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
          event.preventDefault();
          moveFocus(focusIndex + 1);
        } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
          event.preventDefault();
          moveFocus(focusIndex - 1);
        } else if (event.key === 'Home') {
          event.preventDefault();
          moveFocus(0);
        } else if (event.key === 'End') {
          event.preventDefault();
          moveFocus(entries.length - 1);
        }
      }}
    >
      <div
        ref={scrollerRef}
        onScroll={syncScrollState}
        className="h-full overflow-y-auto overscroll-contain"
        style={{ scrollbarWidth: 'none', ...railMask(scrollLimits.up, scrollLimits.down) }}
      >
        <div className="relative" style={{ height: naturalHeight }}>
          {entries.map((entry, index) => {
            const active = entry.key === activeKey;
            const previewing = entry.key === previewKey;
            const width = markWidth(active, previewing);
            // Always one mark in the tab order, even if the list just shrank
            // under a focus index the reader left further down.
            const tabStop = index === Math.min(focusIndex, entries.length - 1);
            return (
              <div
                key={entry.key}
                className="absolute right-0"
                style={{
                  top: markCenter(index),
                  width: MARK_HIT_WIDTH_PX,
                  height: TURN_PITCH_PX,
                  transform: 'translateY(-50%)',
                }}
              >
                <button
                  type="button"
                  ref={(element) => {
                    markRefs.current[index] = element;
                  }}
                  tabIndex={tabStop ? 0 : -1}
                  aria-label={t('turnNavigation.jump', { turn: entry.displayNo })}
                  aria-current={active ? 'true' : undefined}
                  aria-describedby={previewing ? previewId : undefined}
                  onFocus={() => {
                    setFocusIndex(index);
                    setPreviewKey(entry.key);
                  }}
                  onClick={() => {
                    onNavigate(entry.key);
                  }}
                  onPointerEnter={() => {
                    setPreviewKey(entry.key);
                  }}
                  className="group flex h-full w-full items-center justify-end"
                >
                  <span
                    aria-hidden="true"
                    className={`block h-0.5 rounded-full transition-all duration-150 motion-reduce:transition-none ${
                      active
                        ? 'bg-gray-900 dark:bg-gray-100'
                        : previewing
                          ? 'bg-gray-500 dark:bg-gray-400'
                          : 'bg-gray-300 dark:bg-white/25'
                    } group-focus-visible:bg-crimson dark:group-focus-visible:bg-crimson-light`}
                    style={{ width }}
                  />
                </button>
              </div>
            );
          })}
        </div>
      </div>
      {preview === undefined ? null : (
        <div
          id={previewId}
          role="tooltip"
          // Flush with the rail and padded on the side facing it: the padding
          // is a bridge the pointer can cross, where a positional gap belongs
          // to neither the rail nor the card and ends the preview in transit.
          className="pointer-events-auto absolute"
          style={{
            right: '100%',
            top: previewOffset(previewIndex, scrollTop, railHeight),
            paddingRight: PREVIEW_GAP_PX,
          }}
        >
          <div
            className="rounded-[10px] border border-gray-200 bg-white px-3 py-2 text-left shadow-card dark:border-white/10 dark:bg-gray-900"
            style={{ width: PREVIEW_WIDTH_PX, maxWidth: PREVIEW_WIDTH_PX }}
          >
            <TurnPreview entry={preview} />
          </div>
        </div>
      )}
    </nav>
  );
}

/**
 * Memoized because the rail renders two host elements per turn while the page
 * re-renders on every streamed event: without the guard a long conversation
 * would rebuild every mark per commit.
 */
export const TurnRail = memo(TurnRailRail);

/**
 * The same turns as a list, for narrow columns, touch input and anyone who
 * would rather click a row than a hairline. It never introduces a second
 * grouping: the rows are the rail's entries, in the rail's order.
 */
export function TurnDirectory({ entries, activeKey, onNavigate }: TurnNavigationProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    // The trigger and the panel both stand down below two turns; a listener
    // that outlived them would keep swallowing Escape, and Escape is also how
    // the running task is stopped.
    if (!open || entries.length < 2) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Node ? event.target : null;
      if (target !== null && containerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setOpen(false);
      // Escape puts the reader back on the control they opened, not at the top
      // of the page.
      buttonRef.current?.focus();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [entries.length, open]);

  if (entries.length < 2) return null;

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className={`rounded-lg px-2.5 py-1 transition-colors ${
          open
            ? 'bg-gray-100 font-medium text-gray-900 dark:bg-white/5 dark:text-gray-50'
            : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200'
        }`}
      >
        {t('turnNavigation.directory')}
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label={t('turnNavigation.directoryLabel')}
          className="absolute right-0 top-full z-30 mt-2 max-h-[min(70vh,420px)] w-72 overflow-y-auto overscroll-contain rounded-xl border border-gray-200 bg-white py-1 shadow-card dark:border-white/10 dark:bg-gray-900"
        >
          {entries.map((entry) => {
            const active = entry.key === activeKey;
            return (
              <button
                key={entry.key}
                type="button"
                aria-current={active ? 'true' : undefined}
                onClick={() => {
                  setOpen(false);
                  onNavigate(entry.key);
                  // The row is unmounted by the line above; without this the
                  // keyboard reader falls back to the top of the page.
                  buttonRef.current?.focus();
                }}
                className={`flex min-h-[44px] w-full flex-col items-start justify-center gap-0.5 px-3 py-2 text-left transition-colors hover:bg-gray-50 dark:hover:bg-white/5 ${
                  active ? 'bg-gray-50 dark:bg-white/5' : ''
                }`}
              >
                <span className="flex w-full items-baseline gap-2">
                  <span className="shrink-0 text-[11px] text-gray-400 dark:text-gray-500">
                    {t('turnNavigation.turn', { turn: entry.displayNo })}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-gray-900 dark:text-gray-100">
                    {entry.promptPreview || t('turnNavigation.turn', { turn: entry.displayNo })}
                  </span>
                  {entry.state === undefined ? null : (
                    <span className="shrink-0 text-[11px] text-gray-400 dark:text-gray-500">
                      {t(STATE_KEYS[entry.state])}
                    </span>
                  )}
                </span>
                {entry.responsePreview ? (
                  <span className="line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
                    {entry.responsePreview}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The way back to the newest reply, and the news that there is one.
 *
 * Deliberately two controls' worth of meaning in one: returning to the end is
 * how following resumes, so the count of what was missed never has to be
 * maintained as a number the reader would only ever mis-read.
 */
export function BackToBottom({
  hasNewContent,
  onReturn,
}: {
  hasNewContent: boolean;
  onReturn: () => void;
}) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onReturn}
      className="pointer-events-auto inline-flex items-center gap-1.5 rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-card transition-colors hover:bg-gray-50 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-white/5"
    >
      {hasNewContent ? (
        <span className="h-1.5 w-1.5 rounded-full bg-crimson dark:bg-crimson-light" />
      ) : null}
      {t(hasNewContent ? 'turnNavigation.newContent' : 'turnNavigation.backToBottom')}
      <svg
        aria-hidden="true"
        className="h-3.5 w-3.5"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14m0 0 6-6m-6 6-6-6" />
      </svg>
    </button>
  );
}
