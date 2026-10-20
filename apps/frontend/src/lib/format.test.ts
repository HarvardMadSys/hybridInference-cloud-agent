import { describe, expect, it } from 'vitest';

import { formatDuration, shortDay } from './format';

describe('formatDuration', () => {
  it('reads durations the way a person would say them', () => {
    expect(formatDuration(420)).toBe('420ms');
    expect(formatDuration(4_200)).toBe('4.2s');
    expect(formatDuration(42_000)).toBe('42s');
    expect(formatDuration(125_000)).toBe('2m 5s');
    expect(formatDuration(130_000)).toBe('2m 10s');
  });

  it('refuses to render a number it was not given', () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
  });

  it('carries rounding across the unit boundary rather than printing past it', () => {
    // Just under a minute, which rounds to 60 seconds before it becomes one.
    expect(formatDuration(59_960)).toBe('1m');
    expect(formatDuration(119_600)).toBe('2m');
  });
});

describe('shortDay', () => {
  it('names a UTC day briefly', () => {
    expect(shortDay('2026-09-23')).toBe('Sep 23');
    expect(shortDay('2026-09-23T23:30:00Z')).toBe('Sep 23');
    expect(shortDay('soon')).toBe('—');
    expect(shortDay(undefined)).toBe('—');
  });
});
