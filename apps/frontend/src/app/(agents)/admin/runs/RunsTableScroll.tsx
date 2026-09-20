'use client';

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { CARD } from './shared';
import styles from './RunsTableScroll.module.css';

/** A native scrollbar that stays within reach while reading a long table. */
export function RunsTableScroll({ children }: { children: ReactNode }) {
  const id = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const scrollbar = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, max: 0 });

  const measure = useCallback(() => {
    const element = viewport.current;
    if (!element) return;
    const max = Math.max(0, element.scrollWidth - element.clientWidth);
    const left = Math.max(0, Math.min(element.scrollLeft, max));
    setPosition((current) =>
      current.left === left && current.max === max ? current : { left, max },
    );
  }, []);

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(element);
    if (element.firstElementChild) observer?.observe(element.firstElementChild);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [measure]);

  useEffect(() => {
    // Synchronize after the track's scroll width updates on resize.
    if (scrollbar.current) scrollbar.current.scrollLeft = position.left;
  }, [position]);

  function scrollTo(left: number) {
    if (!viewport.current) return;
    viewport.current.scrollLeft = left;
    measure();
  }

  const overflowing = position.max > 1;
  return (
    <div className={`${CARD} relative isolate mt-3`}>
      {overflowing && (
        <div className="sticky top-0 z-20 rounded-t-2xl border-b border-gray-100 bg-white px-3 py-1.5 dark:border-white/5 dark:bg-gray-900">
          <div
            ref={scrollbar}
            role="scrollbar"
            aria-label="Scroll runs horizontally"
            aria-controls={id}
            aria-orientation="horizontal"
            aria-valuemin={0}
            aria-valuemax={position.max}
            aria-valuenow={Math.round(position.left)}
            tabIndex={0}
            className={styles.scrollbar}
            onScroll={(event) => scrollTo(event.currentTarget.scrollLeft)}
            onKeyDown={(event) => {
              const element = viewport.current;
              if (!element) return;
              const step = element.clientWidth * 0.75;
              const destinations: Record<string, number> = {
                ArrowLeft: element.scrollLeft - 48,
                ArrowRight: element.scrollLeft + 48,
                PageUp: element.scrollLeft - step,
                PageDown: element.scrollLeft + step,
                Home: 0,
                End: position.max,
              };
              if (Object.hasOwn(destinations, event.key)) {
                event.preventDefault();
                scrollTo(destinations[event.key]);
              }
            }}
          >
            <div
              aria-hidden="true"
              className="h-px"
              style={{ width: `calc(100% + ${position.max}px)` }}
            />
          </div>
        </div>
      )}
      <div
        ref={viewport}
        id={id}
        role="region"
        aria-label="Runs table"
        tabIndex={0}
        onScroll={measure}
        className={`relative overflow-x-auto rounded-b-2xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson dark:focus-visible:ring-crimson-light ${overflowing ? '' : 'rounded-t-2xl'}`}
      >
        {children}
      </div>
    </div>
  );
}
