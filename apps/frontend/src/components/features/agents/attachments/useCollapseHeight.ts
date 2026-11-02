'use client';

import { useLayoutEffect, useRef, useState, type RefObject } from 'react';

/**
 * Make room for the attachment strip, and give it back, without a jump.
 *
 * The composer is a column: anything the strip gains or loses — a first
 * picture arriving, the last one being removed, one more thumbnail pushing the
 * row onto a second line — moves the message box and everything under it.
 * Animating that one box's height keeps the change continuous instead of
 * snapping.
 *
 * Only the container's height is animated. Its contents stay React's: a removed
 * attachment is unmounted at once, so nothing here holds a stale entry or a
 * revoked preview URL. The motion decides how tall the space is drawn for the
 * next few frames; it never delays a real add, remove or send.
 */

/** Long enough to read as motion, short enough not to lag behind a fast user. */
const DURATION_MS = 180;

/** The compositor's easing, as used by the rest of the card entrances. */
const EASING = 'cubic-bezier(0.2, 0, 0, 1)';

/**
 * Whether motion is wanted.
 *
 * `prefers-reduced-motion` is honoured here as well as in the stylesheet: the
 * stylesheet's blanket `transition: none` cannot switch off a Web Animation, so
 * an explicit check is what makes the reduced-motion case land immediately.
 */
function motionAllowed(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
  try {
    return !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    // A media query that cannot answer is no reason to make the UI jump.
    return false;
  }
}

export interface CollapseHeight {
  /** The box whose height is animated. */
  frameRef: RefObject<HTMLDivElement>;
  /** The contents; their natural height is the target. */
  contentRef: RefObject<HTMLDivElement>;
  /** True while the strip has contents, or is still closing from having had some. */
  mounted: boolean;
}

/**
 * @param count how many items the strip is currently showing.
 */
export function useCollapseHeight(count: number): CollapseHeight {
  const frameRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = useState(count > 0);
  /** Bumped per motion so a superseded one cannot settle over its successor. */
  const generation = useRef(0);
  const running = useRef<Animation | null>(null);
  /** Set when the strip is appearing; there the only honest start is zero. */
  const growFromZero = useRef(false);
  /** The visual height an interrupted motion was cut at, before it snapped. */
  const interruptedAt = useRef<number | null>(null);
  /**
   * The height the frame rests at once its motion is over. It has to be
   * remembered: this effect runs after the new contents are laid out, and an
   * unstyled frame already measures *their* height by then, so measuring it
   * here would make every change look like no change at all. Unknown (null)
   * until the first measurement, so a strip that mounts with contents does not
   * animate in from nothing.
   */
  const resting = useRef<number | null>(null);

  // The contents can change height without a change in count: a row rewraps
  // when the window is resized, a picture that cannot be decoded becomes a
  // chip, a refused upload turns into its reason. Keep `resting` true to what
  // is on screen, so the next motion starts where the strip really is.
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!mounted || !content || typeof ResizeObserver !== 'function') return;
    const observer = new ResizeObserver(() => {
      // A running motion owns the height; it lands on its own target.
      if (!running.current) resting.current = content.offsetHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [mounted]);

  useLayoutEffect(() => {
    // Appearing: the contents have to exist before they can be measured, so
    // mount them first and let the next layout effect grow from nothing.
    if (count > 0 && !mounted) {
      growFromZero.current = true;
      setMounted(true);
      return;
    }
    if (!mounted) return;
    const frame = frameRef.current;
    const content = contentRef.current;
    if (!frame || !content) return;

    const target = count > 0 ? content.offsetHeight : 0;
    // Start where the box is drawn: at zero when appearing, at the height an
    // interrupted motion had reached, or at the height it was resting at.
    const from = growFromZero.current
      ? 0
      : (interruptedAt.current ?? resting.current ?? frame.getBoundingClientRect().height);
    growFromZero.current = false;
    interruptedAt.current = null;
    resting.current = target;

    const current = ++generation.current;
    running.current?.cancel();
    running.current = null;

    const settle = () => {
      if (generation.current !== current) return;
      frame.style.height = '';
      frame.style.overflow = '';
      frame.style.willChange = '';
      // Only an emptied strip goes away; a motion whose delta is zero (an
      // unmeasurable layout, say) leaves what is drawn alone.
      if (count === 0) setMounted(false);
    };

    // Nothing to animate — no layout engine, reduced motion, or no change at
    // all: land on the final state at once.
    if (typeof frame.animate !== 'function' || !motionAllowed() || from === target) {
      settle();
      return;
    }

    frame.style.overflow = 'hidden';
    frame.style.height = `${target}px`;
    frame.style.willChange = 'height';
    const animation = frame.animate([{ height: `${from}px` }, { height: `${target}px` }], {
      duration: DURATION_MS,
      easing: EASING,
    });
    running.current = animation;
    animation.onfinish = () => {
      if (running.current === animation) running.current = null;
      settle();
    };
    return () => {
      // A new target supersedes this one. Remember where the motion was cut so
      // the next one starts there rather than at the height `cancel` snaps to.
      if (running.current !== animation) return;
      running.current = null;
      interruptedAt.current = frame.getBoundingClientRect().height;
      animation.cancel();
    };
  }, [count, mounted]);

  return { frameRef, contentRef, mounted };
}
