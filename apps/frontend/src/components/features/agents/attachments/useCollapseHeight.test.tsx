// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useCollapseHeight } from './useCollapseHeight';

/**
 * jsdom has no layout engine: every element measures zero. These tests stand in
 * for the two things the hook reads — the content's natural height and the
 * frame's current height — and for the Web Animation the browser would run.
 */

const CONTENT_HEIGHT = 100;

type FakeAnimation = {
  cancel: () => void;
  onfinish: (() => void) | null;
  finished: boolean;
  keyframes: Keyframe[];
};

let animations: FakeAnimation[] = [];

function installAnimationSupport() {
  animations = [];
  Object.defineProperty(HTMLDivElement.prototype, 'animate', {
    configurable: true,
    value(this: HTMLDivElement, keyframes: Keyframe[]) {
      const animation: FakeAnimation = {
        keyframes,
        finished: false,
        onfinish: null,
        cancel: () => {
          animation.finished = true;
        },
      };
      animations.push(animation);
      return animation as unknown as Animation;
    },
  });
}

function removeAnimationSupport() {
  delete (HTMLDivElement.prototype as unknown as { animate?: unknown }).animate;
}

/** Every element reports `CONTENT_HEIGHT` as its content and the frame its drawn height. */
let drawnHeight = 0;
function installMeasurements() {
  drawnHeight = 0;
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get: () => CONTENT_HEIGHT,
  });
  Object.defineProperty(HTMLElement.prototype, 'getBoundingClientRect', {
    configurable: true,
    value(this: HTMLElement) {
      const height = this.hasAttribute('data-frame') ? drawnHeight : CONTENT_HEIGHT;
      return { height, width: 0, top: 0, left: 0, right: 0, bottom: height, x: 0, y: 0 };
    },
  });
}

function Probe({ count }: { count: number }) {
  const { frameRef, contentRef, mounted } = useCollapseHeight(count);
  return (
    <div>
      <div ref={frameRef} data-frame="">
        {mounted ? <div ref={contentRef}>{count > 0 ? 'strip' : null}</div> : null}
      </div>
      <span data-state={String(mounted)} />
    </div>
  );
}

function state(): string {
  return document.querySelector('[data-state]')!.getAttribute('data-state')!;
}

beforeEach(() => {
  installMeasurements();
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false }) as MediaQueryList),
  );
});

afterEach(() => {
  cleanup();
  removeAnimationSupport();
  vi.unstubAllGlobals();
  delete (HTMLElement.prototype as unknown as { offsetHeight?: unknown }).offsetHeight;
  delete (HTMLElement.prototype as unknown as { getBoundingClientRect?: unknown })
    .getBoundingClientRect;
});

describe('useCollapseHeight', () => {
  it('draws nothing while empty, and mounts as soon as something is attached', () => {
    const { rerender } = render(<Probe count={0} />);
    expect(state()).toBe('false');

    installAnimationSupport();
    rerender(<Probe count={1} />);
    expect(state()).toBe('true');
    // The first motion starts at zero: there was no height to grow from.
    expect(animations[0]!.keyframes).toEqual([{ height: '0px' }, { height: '100px' }]);
  });

  it('animates a removal from the height on screen down to nothing', () => {
    installAnimationSupport();
    const { rerender } = render(<Probe count={1} />);
    drawnHeight = CONTENT_HEIGHT;
    animations = [];

    // The contents are already gone; the box closes from the height they had.
    rerender(<Probe count={0} />);
    expect(state()).toBe('true');
    expect(animations).toHaveLength(1);
    expect(animations[0]!.keyframes).toEqual([{ height: '100px' }, { height: '0px' }]);

    act(() => animations[0]!.onfinish?.());
    expect(state()).toBe('false');
  });

  it('continues from where an interrupted motion was, rather than restarting', () => {
    installAnimationSupport();
    const { rerender } = render(<Probe count={1} />);
    drawnHeight = CONTENT_HEIGHT;
    animations = [];

    // A quick remove, then a quick add before the first motion finished.
    rerender(<Probe count={0} />);
    drawnHeight = 40; // where the closing motion had got to
    rerender(<Probe count={1} />);

    // The second motion begins at the cut height, not at zero or at full size.
    expect(animations.at(-1)!.keyframes).toEqual([{ height: '40px' }, { height: '100px' }]);
    // And the superseded motion was cancelled, so it cannot settle later.
    expect(animations[0]!.finished).toBe(true);
  });

  it('treats two items filling the same row as no height change', () => {
    installAnimationSupport();
    const { rerender } = render(<Probe count={1} />);
    drawnHeight = CONTENT_HEIGHT;
    animations = [];

    rerender(<Probe count={2} />);
    // Nothing moved, so no motion is started — and the strip stays up.
    expect(animations).toHaveLength(0);
    expect(state()).toBe('true');
  });

  it('keeps the change continuous when a row wraps onto a second line', () => {
    installAnimationSupport();
    const { rerender } = render(<Probe count={3} />);
    drawnHeight = CONTENT_HEIGHT;
    animations = [];

    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get: () => CONTENT_HEIGHT * 2,
    });
    rerender(<Probe count={4} />);
    expect(animations.at(-1)!.keyframes).toEqual([{ height: '100px' }, { height: '200px' }]);
  });

  it('lands at once under prefers-reduced-motion instead of animating', () => {
    installAnimationSupport();
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => ({ matches: true }) as MediaQueryList),
    );
    const { rerender } = render(<Probe count={1} />);
    drawnHeight = CONTENT_HEIGHT;
    animations = [];

    rerender(<Probe count={0} />);
    // No motion at all, and the space is already closed.
    expect(animations).toHaveLength(0);
    expect(state()).toBe('false');
  });

  it('lands at once where the browser has no Web Animations API', () => {
    removeAnimationSupport();
    const { rerender } = render(<Probe count={1} />);
    drawnHeight = CONTENT_HEIGHT;

    // A first add still needs no motion; a removal must not wait for one to
    // finish either, or the strip would hang on screen forever.
    rerender(<Probe count={0} />);
    expect(state()).toBe('false');
  });
});
