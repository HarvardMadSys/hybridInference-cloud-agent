// @vitest-environment jsdom
//
// Behaviour of the scroll controller itself.
//
// jsdom has no layout, so this file supplies one: the container reports a fixed
// viewport, each turn reports a position from a mutable table, and the composer
// floats over the last hundred pixels. That is enough to pin the decisions the
// hook makes — where a jump lands, which turn the reader is on, whether new
// output follows or holds — without pretending the arithmetic was measured in a
// browser. Real geometry still needs a real browser; see
// `docs/user/conversation-navigation.md`.

import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useConversationScroll } from './useConversationScroll';

const VIEWPORT_HEIGHT = 600;
const COMPOSER_HEIGHT = 100;
const CONTENT_HEIGHT = 2000;
/** Content coordinate of each turn's anchor. */
const TURN_TOPS: Record<string, number> = { t1: 0, t2: 400, t3: 1200 };

/** The layout table the stubs read, so a test can simulate a reflow. */
interface Layout {
  containerHeight: number;
  contentHeight: number;
  turnTops: Record<string, number>;
}

let layout: Layout;

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    FakeResizeObserver.instances.push(this);
  }

  observe(): void {
    // A real ResizeObserver delivers the initial state of everything it
    // observes, which is how the hook learns the page's starting height.
    queueMicrotask(() => this.trigger());
  }

  unobserve(): void {}
  disconnect(): void {}

  /** Report a layout change the way a browser would, after it happened. */
  trigger(): void {
    this.callback([], this as unknown as ResizeObserver);
  }
}

function stubRect(element: HTMLElement, rect: () => { top: number; height: number }): void {
  Object.defineProperty(element, 'getBoundingClientRect', {
    configurable: true,
    value: () => {
      const { top, height } = rect();
      return {
        top,
        bottom: top + height,
        height,
        left: 0,
        right: 800,
        width: 800,
        x: 0,
        y: top,
        toJSON: () => ({}),
      };
    },
  });
}

function Harness({ turnKeys, withProcess = false }: { turnKeys: string[]; withProcess?: boolean }) {
  const scroll = useConversationScroll({ turnKeys, conversationKey: 'conversation' });
  return (
    <div>
      <div ref={scroll.containerRef} data-testid="container">
        <div>
          {turnKeys.map((key) => (
            <section key={key} data-testid={`turn-${key}`} data-turn-key={key}>
              {withProcess && key === 't3' ? (
                <>
                  <div data-testid="answer" data-block-key="t3:answer">
                    Final answer
                  </div>
                  <div data-testid="process" hidden>
                    <div data-testid="process-row" data-block-key="t3:process">
                      Recorded usage
                    </div>
                  </div>
                </>
              ) : null}
            </section>
          ))}
        </div>
        <div ref={scroll.composerRef} data-testid="composer" />
      </div>
      <output data-testid="active">{scroll.activeKey ?? ''}</output>
      <output data-testid="mode">{scroll.mode}</output>
      <output data-testid="away">{String(scroll.awayFromBottom)}</output>
      <output data-testid="new">{String(scroll.hasNewContent)}</output>
      <output data-testid="unavailable">{scroll.unavailableKey ?? ''}</output>
      <button type="button" onClick={() => scroll.jumpToTurn('t1')}>
        jump-first
      </button>
      <button type="button" onClick={() => scroll.jumpToTurn('t3')}>
        jump-last
      </button>
      <button type="button" onClick={() => scroll.jumpToTurn('gone')}>
        jump-missing
      </button>
      <button type="button" onClick={scroll.returnToBottom}>
        bottom
      </button>
    </div>
  );
}

const container = () => screen.getByTestId('container');

/** One animation frame, inside `act`, which is where the hook measures. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => {
      requestAnimationFrame(() => resolve(null));
    });
  });
}

beforeEach(() => {
  layout = {
    containerHeight: VIEWPORT_HEIGHT,
    contentHeight: CONTENT_HEIGHT,
    turnTops: { ...TURN_TOPS },
  };
  FakeResizeObserver.instances = [];
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Render the rail's host, then give it the layout above. */
function renderConversation(turnKeys = ['t1', 't2', 't3'], withProcess = false) {
  render(<Harness turnKeys={turnKeys} withProcess={withProcess} />);
  const region = container();
  Object.defineProperty(region, 'clientHeight', {
    configurable: true,
    get: () => layout.containerHeight,
  });
  Object.defineProperty(region, 'scrollHeight', {
    configurable: true,
    get: () => layout.contentHeight,
  });
  stubRect(region, () => ({ top: 0, height: layout.containerHeight }));
  stubRect(screen.getByTestId('composer'), () => ({
    top: layout.containerHeight - COMPOSER_HEIGHT,
    height: COMPOSER_HEIGHT,
  }));
  for (const key of turnKeys) {
    const turn = screen.getByTestId(`turn-${key}`);
    // The rect is in viewport coordinates: where the turn is in the content,
    // less how far the content has been scrolled.
    stubRect(turn, () => ({
      top: layout.turnTops[key] - region.scrollTop,
      height: 200,
    }));
  }
  if (withProcess) {
    stubRect(screen.getByTestId('answer'), () => ({
      top: layout.turnTops.t3 + 40 - region.scrollTop,
      height: 60,
    }));
    stubRect(screen.getByTestId('process-row'), () =>
      screen.getByTestId('process').hidden
        ? { top: 0, height: 0 }
        : { top: layout.turnTops.t3 + 100 - region.scrollTop, height: 100 },
    );
  }
  return region;
}

describe('opening a conversation', () => {
  it('starts at the end and marks the newest turn', async () => {
    const region = renderConversation();
    await settle();

    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);
    expect(screen.getByTestId('active')).toHaveTextContent('t3');
    expect(screen.getByTestId('mode')).toHaveTextContent('following');
    expect(screen.getByTestId('away')).toHaveTextContent('false');
  });
});

describe('jumping to a turn', () => {
  it('leaves the turn just below the top of the reading area', async () => {
    const region = renderConversation();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();

    // Turn 3 opens at 1200 in the content; the jump asks for 24px of clearance.
    expect(region.scrollTop).toBe(TURN_TOPS.t3 - 24);
    expect(screen.getByTestId('active')).toHaveTextContent('t3');
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });

  it('keeps reading even when the newest turn does not fill the page', async () => {
    layout.contentHeight = layout.containerHeight;
    const region = renderConversation();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();

    // The scroll clamps to the end, which must not be read as "follow again".
    expect(region.scrollTop).toBe(0);
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });

  it('says so instead of landing somewhere else when the turn is gone', async () => {
    renderConversation();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'jump-missing' }));
    await settle();
    await settle();

    expect(screen.getByTestId('unavailable')).toHaveTextContent('gone');
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });
});

describe('reading while the conversation grows', () => {
  it('anchors the visible answer rather than a folded process row during reflow and disclosure', async () => {
    const region = renderConversation(['t1', 't2', 't3'], true);
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();
    const answerOffset = screen.getByTestId('answer').getBoundingClientRect().top;
    // A retained hidden row has a zero browser rect; it must not replace the
    // visible answer as the reading anchor simply because it is later in the DOM.
    expect(screen.getByTestId('process-row').getBoundingClientRect().top).toBe(0);
    layout = {
      ...layout,
      contentHeight: layout.contentHeight + 120,
      turnTops: { ...layout.turnTops, t3: layout.turnTops.t3 + 120 },
    };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();
    expect(screen.getByTestId('answer').getBoundingClientRect().top).toBe(answerOffset);

    const heldAt = region.scrollTop;
    screen.getByTestId('process').hidden = false;
    layout = { ...layout, contentHeight: layout.contentHeight + 100 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();
    expect(region.scrollTop).toBe(heldAt);
    screen.getByTestId('process').hidden = true;
    layout = { ...layout, contentHeight: layout.contentHeight - 100 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();
    expect(screen.getByTestId('answer').getBoundingClientRect().top).toBe(answerOffset);
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });
  it('holds the reader place and reports the new content', async () => {
    const region = renderConversation();
    await settle();

    // Read turn 3, then a longer reply arrives below it.
    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();
    const settledAt = region.scrollTop;

    layout = {
      ...layout,
      contentHeight: layout.contentHeight + 300,
    };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();

    expect(region.scrollTop).toBe(settledAt);
    expect(screen.getByTestId('new')).toHaveTextContent('true');
    expect(screen.getByTestId('away')).toHaveTextContent('true');
  });

  it('follows new output when the reader is at the end', async () => {
    const region = renderConversation();
    await settle();

    layout = { ...layout, contentHeight: layout.contentHeight + 300 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();

    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);
    expect(screen.getByTestId('mode')).toHaveTextContent('following');
  });

  it('keeps the reading position when earlier content reflows', async () => {
    const region = renderConversation();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();
    const anchored = TURN_TOPS.t3 - region.scrollTop;

    // A tool row above the reader's line expands by 120px.
    layout = {
      ...layout,
      contentHeight: layout.contentHeight + 120,
      turnTops: { ...layout.turnTops, t3: layout.turnTops.t3 + 120 },
    };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();

    expect(TURN_TOPS.t3 + 120 - region.scrollTop).toBe(anchored);
  });
});

describe('the hook and the reader together', () => {
  it('does not read its own compensation as the reader returning to the end', async () => {
    const region = renderConversation();
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();

    // A wheel, then a reflow inside the gesture window. The write that holds
    // the reader's place is this hook's, not theirs, and a clamped landing must
    // not be mistaken for the reader asking to follow the end again.
    fireEvent.wheel(region);
    layout = {
      ...layout,
      contentHeight: layout.contentHeight + 120,
      turnTops: { ...layout.turnTops, t3: layout.turnTops.t3 + 120 },
    };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    region.scrollTop = layout.contentHeight - layout.containerHeight;
    fireEvent.scroll(region);
    await settle();

    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });
});

describe('a stale write flag', () => {
  it('still resumes following when the compensation moved nothing', async () => {
    const region = renderConversation();
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'jump-last' }));
    await settle();

    // Output arrives below the reader, so holding their place writes the offset
    // it already has. That write produces no scroll event, and the flag it left
    // behind must not swallow the reader's own next scroll.
    layout = { ...layout, contentHeight: layout.contentHeight + 300 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');

    region.scrollTop = layout.contentHeight - layout.containerHeight;
    fireEvent.wheel(region);
    fireEvent.scroll(region);
    await waitFor(() => expect(screen.getByTestId('mode')).toHaveTextContent('following'));
  });
});

describe('a viewport that shrinks', () => {
  it('keeps following when the reader was at the end', async () => {
    const region = renderConversation();
    await settle();
    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);

    // A shorter window is not the reader leaving the end: the bottom of the
    // page is still what they were looking at.
    layout = { ...layout, containerHeight: 400 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();

    expect(screen.getByTestId('mode')).toHaveTextContent('following');
    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);
  });
});

describe('layout changes before the next scroll frame', () => {
  it('keeps the latest manual position when output arrives before the next frame', async () => {
    const region = renderConversation();
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'jump-first' }));
    await settle();

    fireEvent.wheel(region, { deltaY: 1000 });
    region.scrollTop = 1000;
    fireEvent.scroll(region);
    // Streaming or composer layout can be reported before the queued frame
    // has sampled the reader's new position.
    layout = { ...layout, contentHeight: layout.contentHeight + 120 };
    act(() => FakeResizeObserver.instances.forEach((observer) => observer.trigger()));

    expect(region.scrollTop).toBe(1000);
    await settle();
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
  });

  it('keeps following when reaching the bottom shrinks the content', async () => {
    const region = renderConversation();
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'jump-first' }));
    await settle();

    fireEvent.wheel(region, { deltaY: 1400 });
    region.scrollTop = layout.contentHeight - layout.containerHeight;
    fireEvent.scroll(region);
    // Hiding the back-to-bottom control can shrink the sticky composer.
    // Browsers clamp the scroll offset to the new maximum before notifying
    // ResizeObserver, so it is now below the previous maximum.
    layout = { ...layout, contentHeight: layout.contentHeight - 40 };
    region.scrollTop = layout.contentHeight - layout.containerHeight;
    act(() => FakeResizeObserver.instances.forEach((observer) => observer.trigger()));
    await settle();

    expect(screen.getByTestId('mode')).toHaveTextContent('following');
    layout = { ...layout, contentHeight: layout.contentHeight + 120 };
    act(() => FakeResizeObserver.instances.forEach((observer) => observer.trigger()));
    await settle();
    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);
    expect(screen.getByTestId('away')).toHaveTextContent('false');
  });
});

describe('going back to the bottom', () => {
  it('returns to the end and resumes following', async () => {
    const region = renderConversation();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'jump-first' }));
    await settle();
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');

    fireEvent.click(screen.getByRole('button', { name: 'bottom' }));
    await settle();

    expect(region.scrollTop).toBe(layout.contentHeight - layout.containerHeight);
    expect(screen.getByTestId('mode')).toHaveTextContent('following');
    expect(screen.getByTestId('away')).toHaveTextContent('false');
  });
});

describe('a reader who scrolls', () => {
  it('stops following as soon as they take the scroll wheel', async () => {
    const region = renderConversation();
    await settle();

    region.scrollTop = 400;
    fireEvent.wheel(region);
    fireEvent.scroll(region);
    await settle();

    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
    expect(screen.getByTestId('away')).toHaveTextContent('true');
  });

  it('does not fight a scroll the browser performed itself', async () => {
    const region = renderConversation();
    await settle();

    // Focus moving to a control inside an older turn scrolls it into view with
    // no wheel, touch or scroll key from the reader. Snapping back to the end
    // would fight the focus ring on every frame the run keeps producing output.
    region.scrollTop = 400;
    fireEvent.scroll(region);
    await settle();
    expect(screen.getByTestId('mode')).toHaveTextContent('following');

    layout = { ...layout, contentHeight: layout.contentHeight + 300 };
    FakeResizeObserver.instances.forEach((observer) => observer.trigger());
    await settle();

    expect(region.scrollTop).toBe(400);
    expect(screen.getByTestId('mode')).toHaveTextContent('reading');
    expect(screen.getByTestId('active')).toHaveTextContent('t2');
  });

  it('follows again once they scroll back to the end themselves', async () => {
    const region = renderConversation();
    await settle();

    region.scrollTop = 400;
    fireEvent.wheel(region);
    fireEvent.scroll(region);
    await settle();

    region.scrollTop = layout.contentHeight - layout.containerHeight;
    fireEvent.wheel(region);
    fireEvent.scroll(region);
    await waitFor(() => expect(screen.getByTestId('mode')).toHaveTextContent('following'));
  });
});
