// Tests for the scroll arithmetic behind the rail.
//
// The DOM shell around these functions needs a real browser to mean anything —
// jsdom reports every box as zero-height — so what is pinned here is the part
// that decides where a click lands and which mark is current, which is exactly
// the part a regression would move silently.

import { describe, expect, it } from 'vitest';

import {
  activeTurnKey,
  clampScrollTop,
  maxScrollTop,
  readingAnchorOf,
  readingLine,
  restoreScrollTop,
  shouldHoldReading,
  turnScrollTop,
  TURN_SCROLL_OFFSET_PX,
  type BlockBox,
} from './useConversationScroll';

const TURNS = [
  { key: 't1', top: 0 },
  { key: 't2', top: 400 },
  { key: 't3', top: 1200 },
];

describe('scroll bounds', () => {
  it('never asks for a negative reach', () => {
    expect(maxScrollTop(100, 400)).toBe(0);
    expect(maxScrollTop(1400, 400)).toBe(1000);
  });

  it('clamps a destination into the reachable range', () => {
    expect(clampScrollTop(-40, 1000)).toBe(0);
    expect(clampScrollTop(4000, 1000)).toBe(1000);
    expect(clampScrollTop(500, 1000)).toBe(500);
    expect(clampScrollTop(Number.NaN, 1000)).toBe(0);
  });

  it('leaves the requested gap above a turn', () => {
    expect(turnScrollTop(400, 1000)).toBe(400 - TURN_SCROLL_OFFSET_PX);
    // At the very start there is nothing above the turn to scroll past.
    expect(turnScrollTop(10, 1000)).toBe(0);
  });
});

describe('reading line', () => {
  it('sits a fixed distance down a tall reading area', () => {
    expect(readingLine(500, 2000)).toBe(500 + 96);
  });

  it('follows a short reading area instead of a fixed inset', () => {
    expect(readingLine(0, 300)).toBeCloseTo(60);
  });
});

describe('activeTurnKey', () => {
  it('picks the last turn that started above the line', () => {
    expect(activeTurnKey(TURNS, 500, false)).toBe('t2');
  });

  it('keeps a long turn current after its own question scrolls away', () => {
    // Reading deep inside turn 2: its opening is far above the line, and the
    // next turn has not started yet.
    expect(activeTurnKey(TURNS, 1100, false)).toBe('t2');
  });

  it('picks the first turn before the line reaches it', () => {
    expect(activeTurnKey(TURNS, -100, false)).toBe('t1');
    expect(activeTurnKey(TURNS, 0, false)).toBe('t1');
  });

  it('picks the newest turn at the end, however short the page is', () => {
    // Short content cannot be scrolled far enough to line a turn up under the
    // reading line; sitting at the end is the honest answer.
    expect(activeTurnKey(TURNS, 5, true)).toBe('t3');
  });

  it('has no answer without turns', () => {
    expect(activeTurnKey([], 100, false)).toBeNull();
    expect(activeTurnKey([], 100, true)).toBeNull();
  });
});

describe('reading anchors', () => {
  const blocks: BlockBox[] = [
    { key: 'b1', top: 100 },
    { key: 'b2', top: 500 },
    { key: 'b3', top: 1300 },
  ];

  it('anchors to the row under the reading line', () => {
    const anchor = readingAnchorOf(TURNS, blocks, 420, 500);
    expect(anchor).toMatchObject({ blockKey: 'b2', turnKey: 't2', turnIndex: 1 });
    // Restoring puts that row back at the same distance from the top.
    expect(restoreScrollTop(anchor!, TURNS, blocks, 5000)).toBe(420);
  });

  it('holds the reader place when content above grows', () => {
    const anchor = readingAnchorOf(TURNS, blocks, 420, 500);
    // Everything above the anchored row shifted down by 300px.
    const grown = blocks.map((block) => ({ ...block, top: block.top + 300 }));
    expect(restoreScrollTop(anchor!, TURNS, grown, 5000)).toBe(720);
  });

  it('falls back to the turn opening when the row disappears', () => {
    // An attempt switch or a folded tool result can take the row with it.
    const anchor = readingAnchorOf(TURNS, blocks, 420, 500);
    const withoutRow = blocks.filter((block) => block.key !== 'b2');
    expect(restoreScrollTop(anchor!, TURNS, withoutRow, 5000)).toBe(turnScrollTop(400, 5000));
  });

  it('falls back to the nearest turn of a shrunk page', () => {
    const anchor = readingAnchorOf(TURNS, blocks, 1200, 1250);
    expect(anchor).toMatchObject({ turnKey: 't3', turnIndex: 2 });
    const shrunk = TURNS.slice(0, 2);
    expect(restoreScrollTop(anchor!, shrunk, [], 5000)).toBe(turnScrollTop(400, 5000));
  });

  it('has nothing to anchor without turns', () => {
    expect(readingAnchorOf([], blocks, 0, 100)).toBeNull();
    expect(restoreScrollTop({ turnKey: 'gone', turnIndex: 0, offset: 0 }, [], [], 100)).toBeNull();
  });
});

describe('shouldHoldReading', () => {
  const turn = 'ajob_1';

  it('holds the position when the reader picks an older run', () => {
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 2 }, { jobId: turn, attemptNo: 1 }, 1, 2),
    ).toBe(true);
  });

  it('does not hold it when a queued turn starts and gains its first attempt', () => {
    // Nothing the reader did: the turn moved on by itself, and the output that
    // follows must still be followed.
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 0 }, { jobId: turn, attemptNo: 1 }, null, 1),
    ).toBe(false);
  });

  it('does not hold it when a retry arrives and becomes the live run', () => {
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 1 }, { jobId: turn, attemptNo: 2 }, null, 2),
    ).toBe(false);
  });

  it('does not hold it when the reader asks for the live run', () => {
    // Choosing the newest attempt is not settling on a fixed older one.
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 1 }, { jobId: turn, attemptNo: 2 }, 2, 2),
    ).toBe(false);
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 0 }, { jobId: turn, attemptNo: 0 }, 0, 1),
    ).toBe(false);
  });

  it('does not carry a choice across to the next turn', () => {
    expect(
      shouldHoldReading({ jobId: 'ajob_0', attemptNo: 1 }, { jobId: 'ajob_1', attemptNo: 1 }, 1, 2),
    ).toBe(false);
    expect(
      shouldHoldReading({ jobId: turn, attemptNo: 1 }, { jobId: turn, attemptNo: 1 }, 1, 2),
    ).toBe(false);
  });
});
