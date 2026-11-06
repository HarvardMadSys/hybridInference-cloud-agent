import { describe, expect, it } from 'vitest';

import { translate, type Translator } from '@/lib/i18n/translate';
import { activityLabel } from './relativeTime';

const now = Date.parse('2026-09-10T12:00:00Z');
const ago = (ms: number) => new Date(now - ms).toISOString();
const en: Translator = (key, vars) => translate('en', key, vars);
const zh: Translator = (key, vars) => translate('zh-CN', key, vars);

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('activityLabel', () => {
  it('shortens recent activity to a unit a sidebar row can fit', () => {
    expect(activityLabel(ago(20_000), now, en)).toBe('now');
    expect(activityLabel(ago(3 * MINUTE + 59_000), now, en)).toBe('3m');
    expect(activityLabel(ago(5 * HOUR), now, en)).toBe('5h');
    expect(activityLabel(ago(23 * HOUR + 59 * MINUTE), now, en)).toBe('23h');
    expect(activityLabel(ago(2 * DAY), now, en)).toBe('2d');
    expect(activityLabel(ago(6 * DAY + 23 * HOUR), now, en)).toBe('6d');
  });

  it('falls back to a date once a week has passed, with the year only when it differs', () => {
    // Local-time calendar dates, so the day may sit either side of midnight UTC.
    expect(activityLabel(ago(30 * DAY), now, en)).toMatch(/^8\/1[12]$/);
    expect(activityLabel(ago(400 * DAY), now, en)).toMatch(/^8\/[67]\/2025$/);
    expect(activityLabel(ago(400 * DAY), now, zh)).toMatch(/^2025\/8\/[67]$/);
  });

  it('speaks the reader’s language', () => {
    expect(activityLabel(ago(20_000), now, zh)).toBe('刚刚');
    expect(activityLabel(ago(3 * MINUTE), now, zh)).toBe('3分钟');
    expect(activityLabel(ago(5 * HOUR), now, zh)).toBe('5小时');
    expect(activityLabel(ago(2 * DAY), now, zh)).toBe('2天');
  });

  it('shows nothing rather than a wrong time', () => {
    expect(activityLabel(null, now, en)).toBeNull();
    expect(activityLabel(undefined, now, en)).toBeNull();
    expect(activityLabel('not a date', now, en)).toBeNull();
    // A clock skewed slightly ahead of the server still reads as "now".
    expect(activityLabel(ago(-30_000), now, en)).toBe('now');
  });
});
