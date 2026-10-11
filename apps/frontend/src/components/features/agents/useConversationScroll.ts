'use client';

// Owns every write to the Chat transcript's scroll position.
//
// The rail, the directory panel and the back-to-bottom control only *ask* this
// hook to move; nothing else in the view calls `scrollTo`, and nothing else
// decides which turn the reader is on. Keeping that in one place is what makes
// "follow the newest reply" and "leave me where I am reading" two states of one
// machine instead of two effects fighting over `scrollTop`.
//
// The geometry lives in pure functions above the hook so the arithmetic that
// decides where a click lands and which mark is current is testable without a
// browser; the hook is the DOM shell around it.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/** Distance between the turn's opening and the top of the reading area. */
export const TURN_SCROLL_OFFSET_PX = 24;
/** Reading line from the top of the visible area, capped so a tall window does not push it down. */
const READING_LINE_MAX_PX = 96;
const READING_LINE_FRACTION = 0.2;
/** Slack for sub-pixel layout rounding when asking whether the end is reached. */
const BOTTOM_EPSILON_PX = 2;
/**
 * How long after a wheel, touch, drag or scroll key a scroll event still counts
 * as the reader's own. Long enough to cover momentum, short enough that a
 * layout-driven scroll is never mistaken for a reader arriving at the end.
 */
const GESTURE_WINDOW_MS = 500;

export type ConversationScrollMode = 'following' | 'reading' | 'jumping';

/** One turn's anchor, in the scroll container's content coordinates. */
export interface TurnBox {
  key: string;
  top: number;
  element: HTMLElement;
}

/** One rendered row's anchor, in the scroll container's content coordinates. */
export interface BlockBox {
  key: string;
  top: number;
}

/**
 * Where the reader is, in terms that survive a layout change.
 *
 * A row under the reading line is the precise answer; the turn that owns it is
 * kept beside the row so a row that disappears — an attempt switch, a folded
 * tool result — still falls back inside the turn the reader was reading.
 */
export interface ReadingAnchor {
  blockKey?: string;
  turnKey: string;
  /** Position of that turn in the transcript, for the clamped fallback. */
  turnIndex: number;
  /** The anchored element's top minus the scroll offset when it was captured. */
  offset: number;
}

interface ViewportMetrics {
  /** Reading width of the Chat column in CSS pixels. */
  width: number;
  /** Full height of the scroll viewport. */
  height: number;
  /** Height above the sticky composer: what a reader can actually see. */
  readingHeight: number;
  /** Height the sticky composer covers at the foot of the viewport. */
  composerHeight: number;
}

export interface ConversationScrollViewport extends ViewportMetrics {
  /** Whether the pointer is precise enough for the rail's thin marks. */
  finePointer: boolean;
}

export interface ConversationScrollOptions {
  /** Turn anchors in transcript order. Their keys are the rail's entries. */
  turnKeys: readonly string[];
  /** Identity of the conversation on screen; a change resets the position. */
  conversationKey: string;
  /** False while another view occupies the transcript region. */
  active?: boolean;
}

export interface ConversationScroll {
  containerRef: (element: HTMLElement | null) => void;
  /** The sticky composer, whose height is not part of the reading area. */
  composerRef: (element: HTMLElement | null) => void;
  viewport: ConversationScrollViewport;
  /** Turn the reader is on, or null before the first measurement. */
  activeKey: string | null;
  mode: ConversationScrollMode;
  /** True while the transcript's end is off screen. */
  awayFromBottom: boolean;
  /** New content arrived while the reader was away from the end. */
  hasNewContent: boolean;
  /** Turn whose jump found nothing to land on; cleared by the next jump. */
  unavailableKey: string | null;
  jumpToTurn: (key: string) => void;
  returnToBottom: () => void;
  /** Follow the newest content again, after the reader's own turn lands. */
  followLatest: () => void;
  /** Hold the current position as a reading position, whatever it was before. */
  holdReading: () => void;
}

/** Clamp a desired scroll offset to what the container can actually reach. */
export function clampScrollTop(top: number, maxScroll: number): number {
  if (!Number.isFinite(top)) return 0;
  return Math.min(Math.max(top, 0), Math.max(0, maxScroll));
}

/** Maximum scroll offset of a container, never negative. */
export function maxScrollTop(scrollHeight: number, clientHeight: number): number {
  return Math.max(0, scrollHeight - clientHeight);
}

/** Where a click on a turn's mark should land. */
export function turnScrollTop(anchorTop: number, maxScroll: number): number {
  return clampScrollTop(anchorTop - TURN_SCROLL_OFFSET_PX, maxScroll);
}

/** Where the reading line sits, in content coordinates, for a given scroll offset. */
export function readingLine(scrollTop: number, readingHeight: number): number {
  return scrollTop + Math.min(READING_LINE_MAX_PX, READING_LINE_FRACTION * readingHeight);
}

/**
 * The turn the reader is on.
 *
 * The last turn that starts at or above the reading line, so a long turn stays
 * current after its own question has scrolled off the top. At the end of the
 * transcript the newest turn wins outright: short content that cannot be
 * scrolled far enough to line up would otherwise leave an older mark lit.
 */
export function activeTurnKey(
  turns: readonly Pick<TurnBox, 'key' | 'top'>[],
  line: number,
  atEnd: boolean,
): string | null {
  if (turns.length === 0) return null;
  if (atEnd) return turns[turns.length - 1].key;
  let active = turns[0].key;
  for (const turn of turns) {
    if (turn.top > line) break;
    active = turn.key;
  }
  return active;
}

/** The anchor a layout change should restore, or null when nothing is anchored. */
export function readingAnchorOf(
  turns: readonly Pick<TurnBox, 'key' | 'top'>[],
  blocks: readonly BlockBox[],
  scrollTop: number,
  line: number,
): ReadingAnchor | null {
  if (turns.length === 0) return null;
  let row: BlockBox | null = null;
  for (const block of blocks) {
    if (block.top > line) break;
    row = block;
  }
  let turnIndex = 0;
  for (let index = 0; index < turns.length; index += 1) {
    if (turns[index].top > line) break;
    turnIndex = index;
  }
  const anchored = row?.top ?? turns[turnIndex].top;
  return {
    ...(row === null ? {} : { blockKey: row.key }),
    turnKey: turns[turnIndex].key,
    turnIndex,
    offset: anchored - scrollTop,
  };
}

/**
 * The scroll offset that puts a reading anchor back where it was.
 *
 * A missing row falls back to the opening of the turn it belonged to, and a
 * missing turn to the nearest one still on the page — a definite landing spot,
 * never the end of the transcript.
 */
export function restoreScrollTop(
  anchor: ReadingAnchor,
  turns: readonly Pick<TurnBox, 'key' | 'top'>[],
  blocks: readonly BlockBox[],
  maxScroll: number,
): number | null {
  if (turns.length === 0) return null;
  if (anchor.blockKey !== undefined) {
    const row = blocks.find((block) => block.key === anchor.blockKey);
    if (row !== undefined) return clampScrollTop(row.top - anchor.offset, maxScroll);
  }
  const turn = turns.find((candidate) => candidate.key === anchor.turnKey);
  if (turn !== undefined) return turnScrollTop(turn.top, maxScroll);
  const nearest = turns[Math.min(anchor.turnIndex, turns.length - 1)];
  return turnScrollTop(nearest.top, maxScroll);
}

/** Which attempt the transcript is showing, and for which turn. */
export interface AttemptSelection {
  jobId: string;
  /** Display number of the selected attempt; 0 means "every attempt". */
  attemptNo: number;
}

/**
 * Whether a change of attempt should hold the reader's position.
 *
 * Only the reader choosing an older run is a reading position. The selection
 * also moves on its own — a queued turn starts and gains its first attempt, a
 * retry arrives and becomes the live one — and that must not stop the newest
 * output from being followed, which is what counting every change did.
 */
export function shouldHoldReading(
  previous: AttemptSelection,
  next: AttemptSelection,
  /** Attempt the reader last asked for, or null if they never chose one. */
  chosenAttemptNo: number | null,
  /** Display number of the newest attempt; 0 when the turn has none yet. */
  liveAttemptNo: number,
): boolean {
  if (previous.jobId !== next.jobId) return false;
  if (previous.attemptNo === next.attemptNo) return false;
  // Not something the reader asked for: the turn moved on by itself.
  if (chosenAttemptNo !== next.attemptNo) return false;
  // Asking for the newest run is not settling on a fixed older one.
  return next.attemptNo !== 0 && next.attemptNo !== liveAttemptNo;
}

/** Distance from the top of the container to an element inside it, in content coordinates. */
function contentTop(element: HTMLElement, container: HTMLElement): number {
  return (
    element.getBoundingClientRect().top -
    container.getBoundingClientRect().top +
    container.scrollTop
  );
}

/**
 * Move to an offset without animating.
 *
 * Every write this hook makes is instant. A smooth animation over a long
 * transcript races two things it cannot win against: the reader's own scrolling,
 * which must never be overridden, and the output still arriving, which changes
 * the destination while the animation is in flight.
 */
function applyScrollTop(container: HTMLElement, top: number): void {
  if (typeof container.scrollTo === 'function') {
    try {
      container.scrollTo({ top, behavior: 'auto' });
      return;
    } catch {
      // Older engines reject the options form; the direct assignment below is
      // the same destination.
    }
  }
  container.scrollTop = top;
}

interface Geometry {
  signature: string;
  turns: TurnBox[];
  blocks: BlockBox[];
}

/** Keys the browser scrolls a focused region with. */
const SCROLL_KEYS = new Set([
  'ArrowUp',
  'ArrowDown',
  'PageUp',
  'PageDown',
  'Home',
  'End',
  ' ',
  'Spacebar',
]);

const EMPTY_VIEWPORT: ViewportMetrics = {
  width: 0,
  height: 0,
  readingHeight: 0,
  composerHeight: 0,
};

/**
 * Keep the Chat readable while it grows: follow the newest reply when the
 * reader is at the end, hold their place when they are not.
 */
export function useConversationScroll({
  turnKeys,
  conversationKey,
  active = true,
}: ConversationScrollOptions): ConversationScroll {
  const containerElement = useRef<HTMLElement | null>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const composerElement = useRef<HTMLElement | null>(null);
  const [composer, setComposer] = useState<HTMLElement | null>(null);

  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [mode, setMode] = useState<ConversationScrollMode>('following');
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [hasNewContent, setHasNewContent] = useState(false);
  const [unavailableKey, setUnavailableKey] = useState<string | null>(null);
  const [viewport, setViewport] = useState<ViewportMetrics>(EMPTY_VIEWPORT);

  const modeRef = useRef<ConversationScrollMode>('following');
  const activeKeyRef = useRef<string | null>(null);
  const conversationRef = useRef<string | null>(null);
  const turnKeysRef = useRef<readonly string[]>(turnKeys);
  const activeRef = useRef(active);
  const geometryRef = useRef<Geometry | null>(null);
  const readingAnchorRef = useRef<ReadingAnchor | null>(null);
  const generationRef = useRef(0);
  const frameRef = useRef<number | null>(null);
  const lastGestureAtRef = useRef(0);
  /** Set by every write this hook makes, consumed by the scroll event it causes. */
  const wroteScrollRef = useRef(false);
  const lastContentHeightRef = useRef(0);
  const lastClientHeightRef = useRef(0);
  const lastWidthRef = useRef(0);
  const lastComposerHeightRef = useRef(0);
  const landAtEndRef = useRef(false);
  const viewportRef = useRef<ViewportMetrics>(EMPTY_VIEWPORT);
  const [finePointer, setFinePointer] = useState(true);

  useEffect(() => {
    turnKeysRef.current = turnKeys;
  }, [turnKeys]);
  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  const containerRef = useCallback((element: HTMLElement | null) => {
    containerElement.current = element;
    setContainer(element);
  }, []);
  const composerRef = useCallback((element: HTMLElement | null) => {
    composerElement.current = element;
    setComposer(element);
  }, []);

  useEffect(() => {
    if (typeof matchMedia !== 'function') return undefined;
    const query = matchMedia('(pointer: fine)');
    const sync = () => setFinePointer(query.matches);
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);

  const measureComposerOcclusion = useCallback((target: HTMLElement): number => {
    const sticky = composerElement.current;
    if (!sticky) return 0;
    const bounds = target.getBoundingClientRect();
    const composerBounds = sticky.getBoundingClientRect();
    // Only the part of the composer that floats over the visible band hides
    // content; on short pages it sits in the flow and hides nothing.
    return Math.max(
      0,
      Math.min(bounds.bottom, composerBounds.bottom) - Math.max(bounds.top, composerBounds.top),
    );
  }, []);

  const readGeometry = useCallback((force: boolean): Geometry | null => {
    const target = containerElement.current;
    if (!target) return null;
    // Deliberately not keyed on `scrollHeight`: rows do not move when the
    // container scrolls, and a layout change invalidates the cache explicitly.
    // Keying on the content height would re-measure every row on every frame a
    // streamed token made the page taller.
    const signature = `${turnKeysRef.current.join('\u0001')}|${target.clientWidth}|${target.clientHeight}`;
    const cached = geometryRef.current;
    if (!force && cached !== null && cached.signature === signature) return cached;

    const turns: TurnBox[] = [];
    for (const element of target.querySelectorAll<HTMLElement>('[data-turn-key]')) {
      const key = element.dataset.turnKey;
      if (key) turns.push({ key, top: contentTop(element, target), element });
    }
    const blocks: BlockBox[] = [];
    for (const element of target.querySelectorAll<HTMLElement>('[data-block-key]')) {
      if (element.closest('[hidden]')) continue;
      const key = element.dataset.blockKey;
      if (key) blocks.push({ key, top: contentTop(element, target) });
    }
    const geometry: Geometry = { signature, turns, blocks };
    geometryRef.current = geometry;
    return geometry;
  }, []);

  /**
   * Move the transcript, and mark the movement as this hook's own.
   *
   * A scroll event cannot say who caused it. Without this, the compensation
   * write that follows a layout change would land inside the window that still
   * counts as the reader's own gesture, and a position the hook chose would be
   * read as the reader asking to follow the end again.
   */
  const writeScrollTop = useCallback((target: HTMLElement, top: number) => {
    // A write that moves nothing produces no scroll event to consume the flag,
    // and the reader's next real scroll would then be mistaken for this hook's
    // own — which is how landing at the end failed to resume following.
    if (Math.abs(target.scrollTop - top) < 1) return;
    wroteScrollRef.current = true;
    applyScrollTop(target, top);
  }, []);

  const publishViewport = useCallback((next: ViewportMetrics) => {
    const current = viewportRef.current;
    if (
      current.width === next.width &&
      current.height === next.height &&
      current.readingHeight === next.readingHeight &&
      current.composerHeight === next.composerHeight
    ) {
      return;
    }
    viewportRef.current = next;
    setViewport(next);
  }, []);

  const setActive = useCallback((next: string | null) => {
    if (activeKeyRef.current === next) return;
    activeKeyRef.current = next;
    setActiveKey(next);
  }, []);

  const enterMode = useCallback((next: ConversationScrollMode) => {
    if (modeRef.current === next) return;
    modeRef.current = next;
    setMode(next);
  }, []);

  const sync = useCallback(
    (force: boolean) => {
      const target = containerElement.current;
      if (!target || !activeRef.current) return;
      const geometry = readGeometry(force);
      if (geometry === null) return;

      const occlusion = measureComposerOcclusion(target);
      const readingHeight = Math.max(0, target.clientHeight - occlusion);
      publishViewport({
        width: target.clientWidth,
        height: target.clientHeight,
        readingHeight,
        composerHeight: occlusion,
      });

      const maxScroll = maxScrollTop(target.scrollHeight, target.clientHeight);

      if (landAtEndRef.current) {
        landAtEndRef.current = false;
        writeScrollTop(target, maxScroll);
      }

      // Read after the landing write: opening at the end must not offer a
      // "back to bottom" control for the frame it took to get there, and
      // whether the end was reached is what decides the mark.
      const atEnd = target.scrollTop >= maxScroll - BOTTOM_EPSILON_PX;
      setAwayFromBottom(!atEnd);
      if (atEnd) setHasNewContent(false);

      // The reading line is where the reader's eye is: a fixed distance below
      // the top of what they can actually see, which is the visible area rather
      // than the container the sticky composer shares.
      const line = readingLine(target.scrollTop, readingHeight);
      const nextActive = activeTurnKey(geometry.turns, line, atEnd);
      setActive(nextActive);
      if (modeRef.current !== 'following') {
        readingAnchorRef.current = readingAnchorOf(
          geometry.turns,
          geometry.blocks,
          target.scrollTop,
          line,
        );
      } else {
        readingAnchorRef.current = null;
      }
      lastContentHeightRef.current = target.scrollHeight;
      lastClientHeightRef.current = target.clientHeight;
    },
    [measureComposerOcclusion, publishViewport, readGeometry, setActive, writeScrollTop],
  );

  const scheduleSync = useCallback(
    (force: boolean) => {
      if (frameRef.current !== null) return;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        sync(force);
      });
    },
    [sync],
  );

  const restoreReadingPosition = useCallback(
    (force: boolean) => {
      const target = containerElement.current;
      const anchor = readingAnchorRef.current;
      if (!target || anchor === null) return;
      const geometry = readGeometry(force);
      if (geometry === null) return;
      const maxScroll = maxScrollTop(target.scrollHeight, target.clientHeight);
      const top = restoreScrollTop(anchor, geometry.turns, geometry.blocks, maxScroll);
      if (top === null) return;
      writeScrollTop(target, top);
    },
    [readGeometry, writeScrollTop],
  );

  /** Start this conversation at its end, the way a chat opens. */
  const resetForConversation = useCallback(() => {
    geometryRef.current = null;
    readingAnchorRef.current = null;
    lastContentHeightRef.current = 0;
    landAtEndRef.current = true;
    generationRef.current += 1;
    setUnavailableKey(null);
    setHasNewContent(false);
    setActive(null);
    enterMode('following');
  }, [enterMode, setActive]);

  const ensureConversation = useCallback(() => {
    if (conversationRef.current === conversationKey) return;
    conversationRef.current = conversationKey;
    resetForConversation();
  }, [conversationKey, resetForConversation]);

  const positionOnTurn = useCallback(
    (key: string): boolean => {
      const target = containerElement.current;
      if (!target) return false;
      const geometry = readGeometry(true);
      if (geometry === null) return false;
      const turn = geometry.turns.find((candidate) => candidate.key === key);
      if (turn === undefined) return false;
      const maxScroll = maxScrollTop(target.scrollHeight, target.clientHeight);
      writeScrollTop(target, turnScrollTop(turn.top, maxScroll));
      // The write above is the new truth for the reading line, and the
      // geometry is still valid — scrolling does not move the rows — so the
      // mark is re-derived from it directly instead of waiting for a frame.
      const line = readingLine(target.scrollTop, viewportRef.current.readingHeight);
      const atEnd = target.scrollTop >= maxScroll - BOTTOM_EPSILON_PX;
      setActive(activeTurnKey(geometry.turns, line, atEnd));
      readingAnchorRef.current = readingAnchorOf(
        geometry.turns,
        geometry.blocks,
        target.scrollTop,
        line,
      );
      setAwayFromBottom(!atEnd);
      return true;
    },
    [readGeometry, setActive, writeScrollTop],
  );

  const jumpToTurn = useCallback(
    (key: string) => {
      const target = containerElement.current;
      if (!target) return;
      // A second click, a session switch or a manual scroll invalidates the
      // intent behind this one: the generation is how a stale callback knows.
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setUnavailableKey(null);
      setHasNewContent(false);
      enterMode('jumping');
      if (positionOnTurn(key)) {
        // Clicking the newest mark still leaves the reader reading: content
        // shorter than the viewport clamps the scroll to the end, and that must
        // not be read as "the reader asked to follow".
        enterMode('reading');
        scheduleSync(true);
        return;
      }
      // The anchor may simply not be committed yet. One retry after the next
      // frame, then the miss is reported rather than papered over by scrolling
      // to a turn the reader did not ask for.
      requestAnimationFrame(() => {
        if (generation !== generationRef.current) return;
        if (positionOnTurn(key)) {
          enterMode('reading');
          scheduleSync(true);
          return;
        }
        setUnavailableKey(key);
        enterMode('reading');
      });
    },
    [enterMode, positionOnTurn, scheduleSync],
  );

  const returnToBottom = useCallback(() => {
    const target = containerElement.current;
    if (!target) return;
    generationRef.current += 1;
    setUnavailableKey(null);
    setHasNewContent(false);
    enterMode('following');
    writeScrollTop(target, maxScrollTop(target.scrollHeight, target.clientHeight));
    scheduleSync(true);
  }, [enterMode, scheduleSync, writeScrollTop]);

  const holdReading = useCallback(() => {
    generationRef.current += 1;
    enterMode('reading');
    scheduleSync(true);
  }, [enterMode, scheduleSync]);

  const followLatest = useCallback(() => {
    generationRef.current += 1;
    setUnavailableKey(null);
    setHasNewContent(false);
    enterMode('following');
    landAtEndRef.current = true;
    scheduleSync(true);
  }, [enterMode, scheduleSync]);

  // One conversation's reading state is not another's. A follow-up keeps the
  // key, so the reader stays where they were across their own next turn.
  const wasActiveRef = useRef(false);
  useEffect(() => {
    ensureConversation();
    if (!active || container === null) {
      wasActiveRef.current = false;
      return;
    }
    // Coming back from another view restores the position the Chat was left at
    // rather than re-deriving it from wherever the scrollbar happened to land.
    // Forced: the cache describes the view that replaced this one.
    if (!wasActiveRef.current) restoreReadingPosition(true);
    wasActiveRef.current = true;
    scheduleSync(true);
  }, [active, container, ensureConversation, restoreReadingPosition, scheduleSync]);

  // Layout changes: a streamed reply, an expanded tool, a wider workspace, a
  // taller composer. None of them may move a reader who is reading.
  useEffect(() => {
    if (container === null || !active) return undefined;
    if (typeof ResizeObserver === 'undefined') return undefined;
    const content = container.firstElementChild;
    const observer = new ResizeObserver(() => {
      if (!activeRef.current) return;
      const height = container.scrollHeight;
      const width = container.clientWidth;
      const occlusion = measureComposerOcclusion(container);
      const maxScroll = maxScrollTop(height, container.clientHeight);
      // Whether the reader was at the end *before* this layout change, judged
      // against the page and the viewport that were there at the time. Asking
      // afterwards answers about a page that has already grown or a window that
      // has already shrunk, so a streamed token would read as "no longer at the
      // end" and a shorter window would silently stop following.
      const previousMaxScroll = maxScrollTop(
        lastContentHeightRef.current,
        lastClientHeightRef.current,
      );
      const wasAtEnd = container.scrollTop >= previousMaxScroll - BOTTOM_EPSILON_PX;
      // Growth that came from a reflow, or from the composer itself, is not
      // something the reader has not been shown. The composer is a sibling of
      // the rows inside the same scroller, so its height has to be excluded by
      // name rather than by assuming only output can make the page taller.
      const grew =
        height > lastContentHeightRef.current &&
        width === lastWidthRef.current &&
        occlusion === lastComposerHeightRef.current;
      lastContentHeightRef.current = height;
      lastClientHeightRef.current = container.clientHeight;
      lastWidthRef.current = width;
      lastComposerHeightRef.current = occlusion;

      // Measure once for this layout; `sync` below reuses this reading.
      readGeometry(true);

      if (modeRef.current === 'following' && wasAtEnd) {
        writeScrollTop(container, maxScroll);
      } else if (modeRef.current === 'following') {
        // Something other than the reader moved the transcript away from the
        // end: focus scrolling an older turn into view, or the browser's own
        // anchor adjustment. Snapping to the end on the next frame would fight
        // them for as long as the run keeps producing output, so where it
        // landed becomes the reading position instead.
        enterMode('reading');
      } else {
        restoreReadingPosition(false);
        if (grew) setHasNewContent(true);
      }
      scheduleSync(false);
    });
    observer.observe(container);
    if (content !== null) observer.observe(content);
    if (composer !== null) observer.observe(composer);
    return () => observer.disconnect();
  }, [
    active,
    composer,
    container,
    enterMode,
    measureComposerOcclusion,
    readGeometry,
    restoreReadingPosition,
    scheduleSync,
    writeScrollTop,
  ]);

  useEffect(() => {
    if (container === null || !active) return undefined;
    const noteGesture = () => {
      lastGestureAtRef.current = performance.now();
      // A gesture is proof that what follows is the reader's own scroll, so any
      // flag an earlier write left behind is stale by definition.
      wroteScrollRef.current = false;
      // The reader's hand outranks a queued jump: cancel it now, not after the
      // delayed scroll sample it would otherwise arrive on.
      generationRef.current += 1;
      // A wheel at the live end has nothing left to reveal, so it does not end
      // following; a wheel away from it does, immediately.
      const maxScroll = maxScrollTop(container.scrollHeight, container.clientHeight);
      const atEnd = container.scrollTop >= maxScroll - BOTTOM_EPSILON_PX;
      if (!atEnd && modeRef.current !== 'reading') enterMode('reading');
    };
    const onScroll = () => {
      if (!activeRef.current) return;
      const target = containerElement.current;
      if (!target) return;
      // The hook's own compensation and jumps are not the reader moving.
      const ours = wroteScrollRef.current;
      wroteScrollRef.current = false;
      const gesture = !ours && performance.now() - lastGestureAtRef.current < GESTURE_WINDOW_MS;
      const maxScroll = maxScrollTop(target.scrollHeight, target.clientHeight);
      const atEnd = target.scrollTop >= maxScroll - BOTTOM_EPSILON_PX;
      if (gesture) {
        // Scrolling back down to the live end is how a reader resumes
        // following; nothing else may decide that for them.
        if (atEnd && modeRef.current === 'reading') enterMode('following');
        else if (!atEnd && modeRef.current !== 'reading') enterMode('reading');
      }
      if (atEnd) setHasNewContent(false);
      scheduleSync(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      // A press on the scrollbar lands on the container itself; anything else
      // is content, and pressing content is not scrolling it.
      if (event.target === container) noteGesture();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      // The composer lives inside this region. Its arrow keys, Home/End and
      // input-method behaviour belong to the field, not to the reader's
      // position in the transcript.
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (SCROLL_KEYS.has(event.key)) noteGesture();
    };
    container.addEventListener('scroll', onScroll, { passive: true });
    container.addEventListener('wheel', noteGesture, { passive: true });
    container.addEventListener('touchstart', noteGesture, { passive: true });
    container.addEventListener('touchmove', noteGesture, { passive: true });
    container.addEventListener('pointerdown', onPointerDown);
    container.addEventListener('keydown', onKeyDown);
    return () => {
      container.removeEventListener('scroll', onScroll);
      container.removeEventListener('wheel', noteGesture);
      container.removeEventListener('touchstart', noteGesture);
      container.removeEventListener('touchmove', noteGesture);
      container.removeEventListener('pointerdown', onPointerDown);
      container.removeEventListener('keydown', onKeyDown);
    };
  }, [active, container, enterMode, scheduleSync]);

  // Nothing may outlive the view: a queued frame from a replaced conversation
  // would otherwise move the next one.
  useEffect(
    () => () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      generationRef.current += 1;
      geometryRef.current = null;
      readingAnchorRef.current = null;
    },
    [conversationKey],
  );

  return useMemo(
    () => ({
      containerRef,
      composerRef,
      viewport: { ...viewport, finePointer },
      activeKey,
      mode,
      awayFromBottom,
      hasNewContent,
      unavailableKey,
      jumpToTurn,
      returnToBottom,
      followLatest,
      holdReading,
    }),
    [
      activeKey,
      awayFromBottom,
      composerRef,
      containerRef,
      finePointer,
      followLatest,
      hasNewContent,
      holdReading,
      jumpToTurn,
      mode,
      returnToBottom,
      unavailableKey,
      viewport,
    ],
  );
}
