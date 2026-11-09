'use client';

import { useEffect, useRef, useState, type CSSProperties } from 'react';

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

/** Whether the visitor asked for less motion; follows the setting while open. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined' && window.matchMedia?.(REDUCED_MOTION).matches === true,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia(REDUCED_MOTION);
    const sync = () => setReduced(query.matches);
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);
  return reduced;
}

const ENTER_MS = 1400;
const FOLLOW = 0.07;
const easeOut = (progress: number) => 1 - (1 - progress) ** 3;

/**
 * The 3D stage: the window settles into place when the page opens, then turns a
 * few degrees toward a mouse pointer. Both run on animation frames that stop
 * once the window is at rest. With reduced motion it simply stands still.
 */
export function useStage(calm: boolean) {
  const scene = useRef<HTMLDivElement>(null);
  const rig = useRef<HTMLDivElement>(null);
  const [entering, setEntering] = useState(!calm);

  useEffect(() => {
    const sceneElement = scene.current;
    const rigElement = rig.current;
    if (!sceneElement || !rigElement || calm) {
      setEntering(false);
      return;
    }
    const state = { x: 0, y: 0, targetX: 0, targetY: 0, enterStart: 0, frame: 0 };
    const set = (name: string, value: string) => rigElement.style.setProperty(name, value);

    const step = (now: number) => {
      state.x += (state.targetX - state.x) * FOLLOW;
      state.y += (state.targetY - state.y) * FOLLOW;
      let settled = 1;
      if (state.enterStart) {
        const progress = Math.min(1, (now - state.enterStart) / ENTER_MS);
        settled = easeOut(progress);
        if (progress >= 1) state.enterStart = 0;
      }
      set('--mx', `${state.x.toFixed(3)}deg`);
      set('--my', `${state.y.toFixed(3)}deg`);
      set('--ex', `${((1 - settled) * 8).toFixed(3)}deg`);
      set('--ty', `${((1 - settled) * 20).toFixed(2)}px`);
      const moving =
        state.enterStart !== 0 ||
        Math.abs(state.targetX - state.x) > 0.004 ||
        Math.abs(state.targetY - state.y) > 0.004;
      state.frame = moving ? window.requestAnimationFrame(step) : 0;
    };
    const kick = () => {
      if (!state.frame) state.frame = window.requestAnimationFrame(step);
    };
    const follow = (event: PointerEvent) => {
      if (event.pointerType !== 'mouse') return;
      const box = sceneElement.getBoundingClientRect();
      state.targetX = ((event.clientX - box.left) / box.width - 0.5) * 8;
      state.targetY = -((event.clientY - box.top) / box.height - 0.5) * 6;
      kick();
    };
    const rest = () => {
      state.targetX = 0;
      state.targetY = 0;
      kick();
    };
    sceneElement.addEventListener('pointermove', follow);
    sceneElement.addEventListener('pointerleave', rest);
    // Two frames, so the starting pose is painted before anything moves.
    const opening = window.requestAnimationFrame(() => {
      state.frame = window.requestAnimationFrame(() => {
        state.frame = 0;
        setEntering(false);
        state.enterStart = performance.now();
        kick();
      });
    });
    return () => {
      window.cancelAnimationFrame(opening);
      window.cancelAnimationFrame(state.frame);
      sceneElement.removeEventListener('pointermove', follow);
      sceneElement.removeEventListener('pointerleave', rest);
    };
  }, [calm]);

  // The pose the window starts from; the animation frames take it from here.
  const startPose = (calm ? {} : { '--ex': '8deg', '--ty': '20px' }) as CSSProperties;
  return { scene, rig, entering, startPose };
}
