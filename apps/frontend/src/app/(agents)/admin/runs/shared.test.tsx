import { describe, expect, it } from 'vitest';

import { ago } from './shared';

describe('list row helpers', () => {
  const now = Date.parse('2026-09-28T12:00:00Z');

  it('says how long ago in the few characters a row can afford', () => {
    expect(ago('2026-09-28T11:59:40Z', now)).toBe('just now');
    expect(ago('2026-09-28T11:56:00Z', now)).toBe('4m ago');
    expect(ago('2026-09-28T07:00:00Z', now)).toBe('5h ago');
    expect(ago('2026-09-25T12:00:00Z', now)).toBe('3d ago');
    // Past a week, "days ago" stops meaning much: the UTC date instead.
    expect(ago('2026-09-14T23:30:00Z', now)).toBe('Sep 14');
    expect(ago(null, now)).toBe('Unavailable');
  });
});
