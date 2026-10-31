import { describe, expect, it } from 'vitest';

import { contextBar, contextHref, opensContext, signedTokens } from './contextPresent';

describe('contextPresent', () => {
  it('links a conversation to its Context view and recognizes the link', () => {
    expect(contextHref('ajob 1')).toBe('/ajob%201?view=context');
    expect(opensContext('?view=context')).toBe(true);
    expect(opensContext('?view=chat')).toBe(false);
    expect(opensContext('')).toBe(false);
  });

  it('draws a conversation against its window only when one was reported', () => {
    expect(contextBar(50_000, 25_000, 200_000, 90_000)).toEqual({
      peak: 0.25,
      latest: 0.125,
      againstWindow: true,
    });
    // Without a window: against the widest shown, and never as a full window.
    expect(contextBar(45_000, null, null, 90_000)).toEqual({
      peak: 0.5,
      latest: null,
      againstWindow: false,
    });
    expect(contextBar(null, null, null, 0)).toEqual({
      peak: 0,
      latest: null,
      againstWindow: false,
    });
  });

  it('signs changes and keeps an unknown one a dash', () => {
    const compact = (value: number) => `${value / 1000}k`;
    expect(signedTokens(12_000, compact)).toBe('+12k');
    expect(signedTokens(-4_000, compact)).toBe('−4k');
    expect(signedTokens(0, compact)).toBe('0');
    expect(signedTokens(null, compact)).toBe('—');
  });
});
